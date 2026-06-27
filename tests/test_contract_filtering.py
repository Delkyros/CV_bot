"""Regression tests for the CLT / contractor pre-filter.

These pin the behaviour added after auditing real jobs the user had to hand-flag
as "Não é CLT" in the web UI. The goal: such jobs are discarded at COLLECTION
time (accepted=False) and never reach the history DB, so there is nothing left
to mark by hand.

LLM-free and network-free: matcher._complete_with_providers is monkeypatched so
the contract classifier is deterministic. We simulate the WORST case — the LLM
wrongly answering "CLT" with high confidence — and assert the textual override
still rejects the job. A separate fixture simulates the LLM being unavailable.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import matcher, text_signals


# A wrong-but-confident "CLT" verdict, used to prove the override beats the LLM.
_LLM_SAYS_CLT = {"regime": "CLT", "confidence": 0.95, "evidence": "Mentions benefits."}


@pytest.fixture
def llm_says_clt(monkeypatch):
    """Force the LLM chain to (wrongly) answer a confident CLT for every job."""
    monkeypatch.setattr(matcher, "_complete_with_providers", lambda *a, **k: dict(_LLM_SAYS_CLT))


@pytest.fixture
def llm_unavailable(monkeypatch):
    """Simulate a total LLM outage (provider chain returns nothing)."""
    monkeypatch.setattr(matcher, "_complete_with_providers", lambda *a, **k: None)


# Real jobs the user flagged "Não é CLT", where the signal is in title+company
# alone (no description needed): "Contract:" titles, contractor marketplaces,
# and USD-hourly rates.
CONTRACTOR_BY_TITLE_COMPANY = [
    ("Contract: AI Operations Specialist", "Newsela"),
    ("LLM Engineer (Remote)", "Hire Feed"),
    ("GenAI Engineer (Remote)", "Hire Feed"),
    ("Remote Data Scientist", "Turing"),
    ("R Engineer | $45/hr Remote", "Crossing Hurdles"),
    ("Software Engineer (Multi-Language) | $40/hr Remote", "Crossing Hurdles"),
]

# Contractor jobs whose signal lives in the description body (representative of
# the foreign-remote roles that have no marker in the title/company).
CONTRACTOR_BY_DESCRIPTION = [
    ("Senior AI Engineer", "Globex", "100% remote contractor role paid at $55/hour. No CLT."),
    ("Data Engineer", "Acme", "Contratação PJ. Necessário CNPJ ativo e emissão de nota fiscal."),
    ("ML Engineer", "Initech", "This is a freelance, self-employed engagement (1099)."),
]

# Legit CLT jobs that must NOT be discarded (false-positive guards). They contain
# words that look suspicious in isolation (budget, contractors, salary figures).
CLT_SAFE = [
    ("Cientista de Dados Pleno", "Hotmart",
     "Vaga efetiva CLT. Forecasting de budget e gestão de contractors externos. "
     "Salário R$120.000/ano, VR, plano de saúde, carteira assinada."),
    ("Engenheiro de ML Sênior", "iFood",
     "Trabalho híbrido em SP. Benefícios CLT, vale-refeição, 13º salário."),
]

# Full set of 19 "Não é CLT" cases from the audit (title+company only, as stored
# in history). Used to pin current coverage so it can only improve, not regress.
ALL_FLAGGED_NON_CLT = CONTRACTOR_BY_TITLE_COMPANY + [
    ("Junior Software Engineer with Accounting Experience (Brazil)", "Sezzle"),
    ("Data Engineer - Work from home - Talent Connection", "Nortal"),
    ("Engenheiro (a) de IA  - Pleno 1", "EY"),
    ("Adtech Engineer - Work from home - Talent Connection", "Nortal"),
    ("Staff AI Engineer", "Medway"),
    ("AI Agent Operations Engineer", "Mindgruve"),
    ("AI Builder, Emerging Talent", "Salesforce"),
    ("Full Stack Automation Engineer", "GigaBrands"),
    ("Senior AI Solutions Engineer (Python & AWS Bedrock)", "Oowlish"),
    ("Engenheiro (a) de IA  - Pleno", "EY"),
    ("Data Engineer II", "Ebury"),
    ("Remote Data Scientist", "Turing"),
]
# Lower bound for the title+company-only detection. The 6 unambiguous cases plus
# the duplicate Turing entry. Raise this as detection improves; it must never drop.
MIN_TITLE_COMPANY_COVERAGE = 7


@pytest.mark.parametrize("title,company", CONTRACTOR_BY_TITLE_COMPANY)
def test_contractor_title_company_rejected_despite_clt_llm(llm_says_clt, title, company):
    result = matcher.classify_contract("", title=title, company=company)
    assert result["accepted"] is False, f"{title} @ {company} should be discarded"
    assert result["inferred_contract_type"] != "CLT"


@pytest.mark.parametrize("title,company,desc", CONTRACTOR_BY_DESCRIPTION)
def test_contractor_description_rejected_despite_clt_llm(llm_says_clt, title, company, desc):
    result = matcher.classify_contract(desc, title=title, company=company)
    assert result["accepted"] is False, f"{title} @ {company} should be discarded"


@pytest.mark.parametrize("title,company,desc", CLT_SAFE)
def test_clt_jobs_not_discarded(llm_says_clt, title, company, desc):
    result = matcher.classify_contract(desc, title=title, company=company)
    assert result["accepted"] is True, f"{title} @ {company} must NOT be discarded"


def test_contractor_rejected_even_when_llm_unavailable(llm_unavailable):
    result = matcher.classify_contract("", title="Dev | $50/hr Remote", company="Toptal")
    assert result["accepted"] is False


def test_clt_kept_when_llm_unavailable(llm_unavailable):
    result = matcher.classify_contract(
        "Vaga CLT com carteira assinada.", title="Data Scientist", company="Magalu"
    )
    assert result["accepted"] is True


def test_title_company_coverage_does_not_regress():
    """Pin how many flagged jobs we catch from title+company alone.

    This is intentionally a lower bound: jobs whose only signal is in the
    description (caught live by the full-text scan) are not counted here.
    """
    caught = [
        (t, c) for t, c in ALL_FLAGGED_NON_CLT
        if text_signals.strong_non_clt_evidence(f"{t} {c}", company=c)
    ]
    assert len(caught) >= MIN_TITLE_COMPANY_COVERAGE, (
        f"Title+company detection regressed: {len(caught)}/{len(ALL_FLAGGED_NON_CLT)} "
        f"caught (expected >= {MIN_TITLE_COMPANY_COVERAGE})."
    )

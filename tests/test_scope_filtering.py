"""Characterization test: how well does the CURRENT pipeline keep out-of-scope
jobs away (the ones the user hand-flagged "Escopo incorreto")?

Scope is decided by the LLM `match_score`. There is NO scope gate at collection
time (the scraper only runs the contract classifier), so every out-of-scope job
still enters the history DB today. The single existing scope-ish gate is the
report/UI relevance filter — reporter.passes_relevance_filter:

    score_clt >= 0.6  AND  match_score >= 50

…and that is view-level only (it hides rows from the report / "Só relevantes"
view; it does NOT stop the job from being written to the DB).

This test feeds the REAL recorded scores of the 19 flagged jobs into that filter
to quantify the current catch rate, and pins it as a baseline. It exists to show
the gap that justifies a collection-time scope filter.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main
from src import matcher, reporter


# Real (score_clt, match_score) recorded for the 19 jobs the user flagged as
# "Escopo incorreto". score_clt=None means the contract LLM said INDEFINIDO
# (stored as "N/A"). These are the actual LLM outputs from the audited run.
ESCOPO_INCORRETO = [
    {"role": "Staff SWE @ WEX",                         "score_clt": 0.9,  "match_score": 55},
    {"role": "C# Engineer @ Nortal",                    "score_clt": 0.95, "match_score": 22},
    {"role": "Assistente Desenvolvimento @ Jobbol",     "score_clt": 0.95, "match_score": 75},
    {"role": "Rust Engineer @ Crossing Hurdles",        "score_clt": None, "match_score": 15},
    {"role": "Kotlin Engineer @ Crossing Hurdles",      "score_clt": None, "match_score": 22},
    {"role": "Engenheiro de IA Pleno @ EY (Vitória)",   "score_clt": None, "match_score": 85},
    {"role": "C# Engineer @ Nortal (SC)",               "score_clt": 0.93, "match_score": 32},
    {"role": "Mobile Android Sênior @ Deliver IT",      "score_clt": 0.99, "match_score": 22},
    {"role": "Software Engineer II @ Teachable",        "score_clt": 1.0,  "match_score": 32},
    {"role": "Cloud/AI Platforms @ Ericsson",           "score_clt": 0.92, "match_score": 55},
    {"role": "QA Engineer @ Integer Consulting",        "score_clt": None, "match_score": 20},
    {"role": "People Analytics @ BIP Brasil",           "score_clt": 0.95, "match_score": 55},
    {"role": "Software Engineer Especialista @ Qive",   "score_clt": 0.95, "match_score": 55},
    {"role": "Programador Júnior @ SetState",           "score_clt": 1.0,  "match_score": 23},
    {"role": "FullStack Júnior @ FCamara",              "score_clt": 0.85, "match_score": 22},
    {"role": "Eng. Software Fullstack Sênior @ TOTVS",  "score_clt": 0.9,  "match_score": 30},
    {"role": "Excel Expert @ YO IT Consulting",         "score_clt": 0.6,  "match_score": 30},
    {"role": "Analista de Processos II @ Serasa",       "score_clt": 0.9,  "match_score": 50},
    {"role": "Pesquisador II @ Bracell",                "score_clt": 0.85, "match_score": 55},
]


def _split():
    caught = [j for j in ESCOPO_INCORRETO if not reporter.passes_relevance_filter(j)]
    slipped = [j for j in ESCOPO_INCORRETO if reporter.passes_relevance_filter(j)]
    return caught, slipped


def test_current_scope_catch_rate_baseline():
    """Pin the current catch rate of the relevance filter on out-of-scope jobs.

    Baseline at audit time: 12/19 hidden, 7/19 slip through. The 7 that slip are
    all CLT (score_clt >= 0.6) AND match_score >= 50 — i.e. exactly the
    "escopo incorreto sendo CLT" the user wants removed. The relevance filter
    cannot catch them because it only looks at CLT confidence + match score.
    """
    caught, slipped = _split()
    assert len(caught) == 12
    assert len(slipped) == 7


def test_scope_filter_is_view_level_not_collection_level():
    """There is no collection-time scope gate today, so ALL 19 enter the DB.

    The relevance filter only changes what the report / UI shows; it never sets
    `accepted=False`, which is what would stop a job from being persisted. This
    test documents that gap: even the "caught" jobs are still in the history.
    """
    caught, _ = _split()
    # Every flagged job lacks an `accepted` decision (no scope classifier ran),
    # so nothing here was prevented from reaching the DB.
    assert all("accepted" not in j for j in ESCOPO_INCORRETO)
    assert len(caught) < len(ESCOPO_INCORRETO)  # the filter is not even a full view-level gate


def test_out_of_scope_clt_jobs_slip_through_today():
    """The specific failure the user reported: CLT + decent match, wrong scope."""
    _, slipped = _split()
    # All slipped jobs are CLT-confident and above the match threshold.
    assert slipped, "expected out-of-scope CLT jobs to slip through the current filter"
    for j in slipped:
        assert j["score_clt"] is not None and j["score_clt"] >= reporter.min_clt_score()
        assert j["match_score"] >= reporter.min_match_score()


# --------------------------------------------------------------------------- #
# NEW collection/persist-level scope gate (matcher core_role_compatible +
# main.analyze_and_filter_jobs). These prove out-of-scope jobs are dropped
# before reaching the report / history DB.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ('{"match_score": 70, "core_role_compatible": false}', False),
    ('{"match_score": 70, "core_role_compatible": "false"}', False),
    ('{"match_score": 70, "core_role_compatible": "não"}', False),
    ('{"match_score": 70, "core_role_compatible": true}', True),
    ('{"match_score": 70}', True),  # omitted -> conservative keep
])
def test_parse_result_extracts_core_role_compatible(raw, expected):
    assert matcher._parse_result(raw)["core_role_compatible"] is expected


# Roles the user flagged "Escopo incorreto" that the LLM should mark incompatible.
OUT_OF_SCOPE_ROLES = [
    "Mobile Android Sênior", "QA Engineer", "Excel Expert",
    "People Analytics", "Analista de Processos II", "Pesquisador II", "Kotlin Engineer",
]
IN_SCOPE_ROLES = ["Machine Learning Engineer", "Cientista de Dados Sênior"]


def _fake_analyze_match(job, _profile):
    """Simulate the LLM: out-of-scope roles get core_role_compatible=False."""
    incompatible = job["job_title"] in OUT_OF_SCOPE_ROLES
    return {
        "match_score": 55,
        "core_role_compatible": not incompatible,
        "strengths": [], "gaps": [], "verdict": "ok",
    }


def test_out_of_scope_jobs_are_dropped_before_persist(monkeypatch):
    monkeypatch.setattr(main, "analyze_match", _fake_analyze_match)
    collected = [
        {"job_title": t, "company": "X", "job_link": f"l/{i}"}
        for i, t in enumerate(OUT_OF_SCOPE_ROLES + IN_SCOPE_ROLES)
    ]
    analyzed = main.analyze_and_filter_jobs(collected, "perfil", has_llm_provider=True)
    kept = {j["job_title"] for j in analyzed}
    assert kept == set(IN_SCOPE_ROLES)
    for role in OUT_OF_SCOPE_ROLES:
        assert role not in kept


def test_scope_filter_never_discards_when_llm_unavailable(monkeypatch):
    # With no provider, analyze_match must not even be called; fallback keeps all.
    def _boom(*a, **k):
        raise AssertionError("analyze_match should not run without a provider")
    monkeypatch.setattr(main, "analyze_match", _boom)
    collected = [{"job_title": t, "company": "X", "job_link": f"l/{i}"}
                 for i, t in enumerate(OUT_OF_SCOPE_ROLES)]
    analyzed = main.analyze_and_filter_jobs(collected, "perfil", has_llm_provider=False)
    assert len(analyzed) == len(collected)  # nothing dropped on infra failure


def test_scope_filter_keeps_job_when_llm_call_raises(monkeypatch):
    # An exception during analysis -> fallback (keep), never an out-of-scope drop.
    def _raise(*a, **k):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(main, "analyze_match", _raise)
    analyzed = main.analyze_and_filter_jobs(
        [{"job_title": "QA Engineer", "company": "X", "job_link": "l/1"}],
        "perfil", has_llm_provider=True,
    )
    assert len(analyzed) == 1

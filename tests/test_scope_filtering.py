"""Tests for the collection/persist-level scope gate that drops out-of-scope
jobs before they reach the report or the history DB.

Two layers, both default-in-scope (a job is dropped only on an explicit
off-track signal): the deterministic TITLE gate (text_signals.out_of_scope_title)
and the LLM `core_role_compatible` judgment wired through
main.analyze_and_filter_jobs.
"""

import pytest

import main
from src import matcher
from src.text_signals import out_of_scope_title


# --------------------------------------------------------------------------- #
# LLM core_role_compatible scope gate, wired through
# main.analyze_and_filter_jobs — proves out-of-scope jobs are dropped before
# reaching the report / history DB.
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


# --------------------------------------------------------------------------- #
# Deterministic TITLE scope gate (text_signals.out_of_scope_title). The real
# out-of-scope roles the user flagged in the web UI, matched by title.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("title", [
    "Desenvolvedor Qlik",
    "Market Intelligence Analyst",
    "Analista de Inteligência de Mercado JR",
    "Analista de sistema",
    "Analista de Sistemas Pleno",
    "Desenvolvedor Junior Node.js - Trabalho Remoto",
    "Associate-Graduate:Developer",
])
def test_out_of_scope_title_flags_off_track_roles(title):
    assert out_of_scope_title(title) is not None


@pytest.mark.parametrize("title", [
    "Cientista de Dados Sênior",
    "Machine Learning Engineer",
    "Engenheiro de Dados",
    "Analista de Dados",
    "Analista de Dados e BI",   # ambiguous -> NOT hard-dropped (left to the LLM)
    "AI Developer",
    "Data Insights - Tech Senior Associate",
])
def test_out_of_scope_title_keeps_in_or_adjacent_roles(title):
    assert out_of_scope_title(title) is None


def test_out_of_scope_title_drops_before_llm(monkeypatch):
    # Title-gated jobs must be dropped without ever calling the LLM.
    def _boom(*a, **k):
        raise AssertionError("analyze_match must not run for a title-gated job")
    monkeypatch.setattr(main, "analyze_match", _boom)
    collected = [
        {"job_title": "Desenvolvedor Qlik", "company": "X", "job_link": "l/1"},
        {"job_title": "Analista de Inteligência de Mercado", "company": "X", "job_link": "l/2"},
    ]
    analyzed = main.analyze_and_filter_jobs(collected, "perfil", has_llm_provider=True)
    assert analyzed == []

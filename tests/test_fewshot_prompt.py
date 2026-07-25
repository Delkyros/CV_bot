"""Tests for few-shot exemplar injection (spec 002, User Story 2).

Two guarantees matter most: (1) with no/insufficient exemplars the prompt is
byte-identical to the pre-feature version and behavior is unchanged; (2) the
injection never alters a drop/keep decision — only the LLM score
(Constitution Principles II & VI).
"""

import pytest

import main
from src import matcher

JOB = {"job_title": "Cientista de Dados", "company": "ACME", "location": "Remoto",
       "full_description": "ML, Python, SQL."}
PROFILE = "Cientista de dados sênior."


# --- Prompt is byte-identical without exemplars (T014 a) ---------------------

def test_prompt_identical_when_no_exemplars():
    baseline = matcher._build_prompt(JOB, PROFILE)
    assert matcher._build_prompt(JOB, PROFILE, None) == baseline
    assert matcher._build_prompt(JOB, PROFILE, "") == baseline
    # The exemplar header must NOT be present in the exemplar-free prompt.
    assert "personally reviewed" not in baseline
    # Response contract intact.
    assert '"match_score"' in baseline and '"core_role_compatible"' in baseline


def test_prompt_includes_block_when_given():
    block = "SENTINEL-EXEMPLAR-BLOCK"
    with_block = matcher._build_prompt(JOB, PROFILE, block)
    baseline = matcher._build_prompt(JOB, PROFILE)
    assert block in with_block
    assert with_block != baseline
    # Inserted before the analysis instructions; contract still intact.
    assert with_block.index(block) < with_block.index("Analysis Instructions:")
    assert '"match_score"' in with_block and '"core_role_compatible"' in with_block


# --- build_fewshot_block: budget + graceful fallback (T014 b,c,d) -------------

def _history(n_pos, n_neg):
    h = {}
    for i in range(n_pos):
        h[f"p{i}"] = {"status": "applied", "job_title": f"Cientista {i}",
                      "company": "ACME", "verdict": "Ótimo fit.", "first_seen_at": f"2026-01-{i+1:02d}"}
    for i in range(n_neg):
        h[f"n{i}"] = {"status": "error", "error_class": "Escopo incorreto",
                      "job_title": f"Analista {i}", "company": "X",
                      "verdict": "Fora de escopo.", "first_seen_at": f"2026-02-{i+1:02d}"}
    return h


def test_block_built_within_budget(monkeypatch):
    monkeypatch.setenv("FEWSHOT_MIN_LABELS", "2")
    monkeypatch.setenv("FEWSHOT_CHAR_BUDGET", "2000")
    block = main.build_fewshot_block(_history(3, 3))
    assert block is not None
    assert len(block) <= 2000
    assert "CHOSE TO APPLY" in block and "REJECTED" in block


def test_block_respects_tight_budget(monkeypatch):
    monkeypatch.setenv("FEWSHOT_MIN_LABELS", "2")
    monkeypatch.setenv("FEWSHOT_CHAR_BUDGET", "80")  # only the intro line fits
    # Too tight for any exemplar bullet → falls back to None (no misleading block).
    assert main.build_fewshot_block(_history(3, 3)) is None


def test_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("FEWSHOT_ENABLED", "false")
    assert main.build_fewshot_block(_history(5, 5)) is None


def test_too_few_labels_returns_none(monkeypatch):
    monkeypatch.setenv("FEWSHOT_ENABLED", "true")
    monkeypatch.setenv("FEWSHOT_MIN_LABELS", "10")
    assert main.build_fewshot_block(_history(1, 1)) is None


def test_selection_error_falls_back(monkeypatch):
    monkeypatch.setenv("FEWSHOT_MIN_LABELS", "2")

    def _boom(_entry):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "label_of", _boom)
    assert main.build_fewshot_block(_history(3, 3)) is None  # no exception escapes


# --- Regression: few-shot never changes drop/keep (T014 e) -------------------

def test_fewshot_does_not_change_drop_keep(monkeypatch):
    received = {}

    def _fake_analyze_match(job, _profile, exemplars=None):
        received["exemplars"] = exemplars
        return {"match_score": 80, "core_role_compatible": True,
                "strengths": [], "gaps": [], "verdict": "ok"}

    monkeypatch.setattr(main, "analyze_match", _fake_analyze_match)
    # No search terms → embedding gate skipped; stub the sims so no model loads.
    monkeypatch.setattr(main, "title_scope_similarity", lambda t, terms: 0.5)
    monkeypatch.setattr(main, "description_similarity", lambda job, prof: 0.5)

    collected = [
        {"job_title": "Cientista de Dados", "company": "A"},
        {"job_title": "Engenheiro de ML", "company": "B"},
    ]

    def kept(exemplars):
        jobs = main.analyze_and_filter_jobs(
            [dict(j) for j in collected], "perfil", has_llm_provider=True,
            search_terms=None, exemplars=exemplars)
        return sorted(j["job_title"] for j in jobs)

    assert kept(None) == kept("SENTINEL-BLOCK")  # identical drop/keep set
    assert received["exemplars"] == "SENTINEL-BLOCK"  # arg really threaded through


# --- Output language is configurable, default Portuguese (US3) ----------------

def test_prompt_output_language_default_portuguese(monkeypatch):
    monkeypatch.delenv("LLM_OUTPUT_LANGUAGE", raising=False)
    prompt = matcher._build_prompt(JOB, PROFILE)
    # Anchor on the output-language sentence, not the input-language note.
    assert "verdict in Portuguese" in prompt


def test_prompt_output_language_override(monkeypatch):
    monkeypatch.setenv("LLM_OUTPUT_LANGUAGE", "English")
    prompt = matcher._build_prompt(JOB, PROFILE)
    assert "verdict in English" in prompt
    assert "verdict in Portuguese" not in prompt
    # The response contract and score field are unchanged by the language knob.
    assert '"match_score"' in prompt and '"core_role_compatible"' in prompt

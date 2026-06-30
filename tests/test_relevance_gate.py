"""Does the CLT + match_score relevance gate still surface the jobs the user
should look at — and how well does it reject the ones they flagged as errors?

Ground truth is the user's OWN manual labels, snapshotted from the job history:
- status == "applied"  -> a job the user acted on  (SHOULD be surfaced)
- error_class set       -> a job the user rejected  (should NOT be surfaced)

Each tuple is the (score_clt, match_score) the models actually produced. This is
a regression/characterization baseline (no live LLM calls). The gate under test
is reporter.passes_relevance_filter:

    score_clt numeric and >= REPORT_MIN_CLT_SCORE (0.6)
        AND match_score >= REPORT_MIN_MATCH_SCORE

pinned here to the target bar (CLT >= 0.6, match >= 70).

Conclusion this test pins down: CLT + score is NECESSARY but NOT SUFFICIENT.
It drops 11/32 jobs the user applied to (false negatives: CLT="N/A" or
match < 70), and it only ever rejects an error when CLT came back "N/A" — every
error with a numeric CLT >= 0.6 and match >= 70 slips through. Scope and
location/model errors therefore need their own gates, not CLT+score.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import reporter

# (score_clt, match_score) for the 32 jobs the user marked APPLIED. None == "N/A".
APPLIED = [
    (0.95, 78), (0.99, 92), (0.95, 78), (1.0, 88), (0.92, 65), (0.95, 85),
    (0.95, 68), (0.95, 85), (None, 78), (0.92, 68), (None, 45), (None, 45),
    (None, 50), (None, 50), (0.97, 72), (0.85, 85), (0.97, 78), (0.85, 82),
    (0.95, 72), (0.85, 72), (0.85, 88), (0.85, 85), (0.9, 88), (1.0, 55),
    (0.95, 68), (0.95, 72), (None, 72), (0.85, 88), (0.95, 72), (0.85, 78),
    (0.9, 88), (0.6, 72),
]

# (error_class, score_clt, match_score) for the jobs the user flagged as errors.
ERRORS = [
    ("Não é CLT", None, 78), ("Não é CLT", 0.65, 78), ("Escopo incorreto", 0.95, 75),
    ("Não é CLT", 0.95, 78), ("Não é CLT", 0.95, 78), ("Não é CLT", None, 78),
    ("Escopo incorreto", None, 85), ("Localidade/Modelo incorreto", 0.95, 85),
    ("Localidade/Modelo incorreto", 0.97, 78), ("Não é CLT", None, 85),
    ("Não é CLT", None, 78), ("Escopo incorreto", None, 85), ("Não é CLT", None, 85),
    ("Escopo incorreto", 0.85, 78), ("Localidade/Modelo incorreto", 0.95, 75),
    ("Escopo incorreto", 0.95, 78), ("Localidade/Modelo incorreto", None, 72),
    ("Não é CLT", None, 82), ("Localidade/Modelo incorreto", 0.85, 78),
    ("Localidade/Modelo incorreto", 0.97, 72), ("Localidade/Modelo incorreto", 0.95, 72),
    ("Escopo incorreto", 0.95, 78), ("Escopo incorreto", None, 78),
    ("Escopo incorreto", None, 78), ("Localidade/Modelo incorreto", 0.85, 85),
    ("Localidade/Modelo incorreto", 0.85, 75), ("Não é CLT", 1.0, 78),
    ("Localidade/Modelo incorreto", 1.0, 78), ("Não é CLT", 0.97, 72),
    ("Localidade/Modelo incorreto", 0.85, 72), ("Localidade/Modelo incorreto", 0.85, 72),
    ("Não é CLT", 0.75, 78), ("Escopo incorreto", 0.95, 72),
    ("Localidade/Modelo incorreto", 0.85, 78), ("Não é CLT", 0.95, 75),
    ("Escopo incorreto", 0.85, 72), ("Escopo incorreto", None, 72),
    ("Localidade/Modelo incorreto", 0.95, 78),
]


@pytest.fixture(autouse=True)
def _pin_target_thresholds(monkeypatch):
    monkeypatch.setenv("REPORT_MIN_CLT_SCORE", "0.6")
    monkeypatch.setenv("REPORT_MIN_MATCH_SCORE", "70")


def _passes(clt, match):
    return reporter.passes_relevance_filter({"score_clt": clt, "match_score": match})


def _num(x):
    return x if isinstance(x, (int, float)) else None


# --------------------------------------------------------------------------- #
# Positive side: jobs the user should look at
# --------------------------------------------------------------------------- #
def test_clearly_good_applied_jobs_are_always_surfaced():
    """A confidently-CLT, well-matched applied job must never be dropped."""
    for clt, match in APPLIED:
        c = _num(clt)
        if c is not None and c >= 0.6 and match >= 70:
            assert _passes(clt, match) is True


def test_applied_recall_is_imperfect_with_documented_false_negatives():
    """The gate hides 11/32 jobs the user actually applied to."""
    surfaced = [(c, m) for c, m in APPLIED if _passes(c, m)]
    assert len(surfaced) == 21
    assert len(APPLIED) - len(surfaced) == 11  # false negatives

    # Two are dropped purely by the conservative CLT gate (CLT="N/A") despite a
    # strong match (>= 70) — good jobs hidden only because the contract LLM said
    # INDEFINIDO.
    na_clt_but_high_match = [
        (c, m) for c, m in APPLIED if _num(c) is None and m >= 70 and not _passes(c, m)
    ]
    assert len(na_clt_but_high_match) == 2

    # The other nine are dropped because the match score is below the bar.
    below_match_bar = [(c, m) for c, m in APPLIED if m < 70 and not _passes(c, m)]
    assert len(below_match_bar) == 9


# --------------------------------------------------------------------------- #
# Negative side: jobs the user rejected
# --------------------------------------------------------------------------- #
def test_clt_score_rejects_errors_only_when_clt_is_na():
    """The ONLY error the relevance gate catches is one whose CLT came back N/A.
    Every error with a numeric CLT >= 0.6 and match >= 70 slips through."""
    numeric_clt_errors_caught = [
        (e, c, m) for e, c, m in ERRORS if _num(c) is not None and not _passes(c, m)
    ]
    assert numeric_clt_errors_caught == []  # CLT+score never catches these


def test_error_catch_rate_baseline():
    """Pin the catch rate per error category (at CLT>=0.6, match>=70)."""
    def caught(category):
        return sum(1 for e, c, m in ERRORS if e == category and not _passes(c, m))

    def total(category):
        return sum(1 for e, c, m in ERRORS if e == category)

    # Only the N/A-CLT subset of each category is caught.
    assert (caught("Escopo incorreto"), total("Escopo incorreto")) == (5, 11)
    assert (caught("Não é CLT"), total("Não é CLT")) == (6, 13)
    assert (caught("Localidade/Modelo incorreto"), total("Localidade/Modelo incorreto")) == (1, 14)

    overall_caught = sum(1 for e, c, m in ERRORS if not _passes(c, m))
    assert overall_caught == 12  # the rest (26/38) slip the CLT+score gate


def test_location_model_errors_need_a_separate_gate():
    """Location/model errors are correctly CLT and in-scope, so CLT+score can
    never catch them — they require the workplace location gate."""
    loc_numeric = [
        (c, m) for e, c, m in ERRORS
        if e == "Localidade/Modelo incorreto" and _num(c) is not None
    ]
    assert loc_numeric  # there are such cases
    assert all(_passes(c, m) for c, m in loc_numeric)

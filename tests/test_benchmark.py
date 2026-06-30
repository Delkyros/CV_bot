"""Unit tests for the free-model benchmark scoring (network-free)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import benchmark_models as bm


# --------------------------------------------------------------------------- #
# eval fixture
# --------------------------------------------------------------------------- #
def test_eval_fixture_loads_and_is_balanced():
    jobs = bm.load_eval_jobs()
    assert len(jobs) >= 8
    for j in jobs:
        assert j.get("job_title")
        assert j.get("full_description")
        assert isinstance(j.get("expected_in_scope"), bool)
    in_scope = sum(1 for j in jobs if j["expected_in_scope"])
    out_scope = len(jobs) - in_scope
    # Roughly balanced so accuracy is meaningful (not skewed to one label).
    assert in_scope >= 3 and out_scope >= 3


# --------------------------------------------------------------------------- #
# build_row
# --------------------------------------------------------------------------- #
def _call(content=None, error=None, total_tokens=100, latency=1.0):
    return {"content": content, "error": error,
            "usage": {"total_tokens": total_tokens}, "latency": latency}


def test_build_row_correct_in_scope():
    job = {"job_title": "DS", "expected_in_scope": True}
    row = bm.build_row("m", job, _call('{"match_score": 85}'), threshold=70)
    assert row["match_score"] == 85
    assert row["predicted_in_scope"] is True
    assert row["correct"] is True
    assert row["total_tokens"] == 100


def test_build_row_correct_out_of_scope():
    job = {"job_title": "Qlik", "expected_in_scope": False}
    row = bm.build_row("m", job, _call('{"match_score": 30}'), threshold=70)
    assert row["predicted_in_scope"] is False
    assert row["correct"] is True


def test_build_row_wrong_when_out_scope_scored_high():
    job = {"job_title": "Qlik", "expected_in_scope": False}
    row = bm.build_row("m", job, _call('{"match_score": 90}'), threshold=70)
    assert row["correct"] is False


def test_build_row_error_has_no_score():
    job = {"job_title": "DS", "expected_in_scope": True}
    row = bm.build_row("m", job, _call(error="HTTP 429"), threshold=70)
    assert row["match_score"] is None
    assert row["correct"] is None
    assert row["error"] == "HTTP 429"
    assert row["parse_fail"] is False


def test_build_row_parse_fail():
    job = {"job_title": "DS", "expected_in_scope": True}
    row = bm.build_row("m", job, _call("not json at all"), threshold=70)
    assert row["match_score"] is None
    assert row["correct"] is None
    assert row["parse_fail"] is True


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #
def test_aggregate_ranks_by_accuracy_then_tokens():
    rows = [
        # model A: 2/2 correct, 200 tokens avg
        {"model": "A", "expected_in_scope": True,  "match_score": 80, "correct": True,  "total_tokens": 200, "latency": 1.0, "error": None, "parse_fail": False},
        {"model": "A", "expected_in_scope": False, "match_score": 20, "correct": True,  "total_tokens": 200, "latency": 1.0, "error": None, "parse_fail": False},
        # model B: 1/2 correct
        {"model": "B", "expected_in_scope": True,  "match_score": 80, "correct": True,  "total_tokens": 50,  "latency": 1.0, "error": None, "parse_fail": False},
        {"model": "B", "expected_in_scope": False, "match_score": 90, "correct": False, "total_tokens": 50,  "latency": 1.0, "error": None, "parse_fail": False},
    ]
    stats = aggregate = bm.aggregate(rows)
    assert [s["model"] for s in stats] == ["A", "B"]  # higher accuracy first
    a = stats[0]
    assert a["accuracy"] == 1.0
    assert a["mean_in_score"] == 80 and a["mean_out_score"] == 20
    assert a["separation"] == 60


def test_aggregate_penalizes_low_coverage():
    # "perfect" answers 2 jobs (both right) but fails the other 8; "reliable"
    # answers all 10 at 90%. Effective accuracy must rank "reliable" first.
    rows = []
    for i in range(2):
        rows.append({"model": "perfect", "expected_in_scope": True, "match_score": 80, "correct": True, "total_tokens": 100, "latency": 1.0, "error": None, "parse_fail": False})
    for i in range(8):
        rows.append({"model": "perfect", "expected_in_scope": True, "match_score": None, "correct": None, "total_tokens": None, "latency": 1.0, "error": "HTTP 429", "parse_fail": False})
    for i in range(9):
        rows.append({"model": "reliable", "expected_in_scope": True, "match_score": 80, "correct": True, "total_tokens": 150, "latency": 1.0, "error": None, "parse_fail": False})
    rows.append({"model": "reliable", "expected_in_scope": False, "match_score": 80, "correct": False, "total_tokens": 150, "latency": 1.0, "error": None, "parse_fail": False})

    stats = bm.aggregate(rows)
    assert stats[0]["model"] == "reliable"           # 9/10 = 90% effective
    perfect = next(s for s in stats if s["model"] == "perfect")
    assert perfect["accuracy"] == 1.0                # perfect quality when it answers
    assert perfect["coverage"] == 0.2                # but only 20% coverage
    assert perfect["effective_accuracy"] == 0.2      # ranked on this


def test_aggregate_tiebreak_prefers_fewer_tokens():
    rows = [
        {"model": "cheap", "expected_in_scope": True, "match_score": 80, "correct": True, "total_tokens": 50,  "latency": 1.0, "error": None, "parse_fail": False},
        {"model": "pricey", "expected_in_scope": True, "match_score": 80, "correct": True, "total_tokens": 500, "latency": 1.0, "error": None, "parse_fail": False},
    ]
    stats = bm.aggregate(rows)
    assert stats[0]["model"] == "cheap"  # same accuracy -> fewer tokens wins

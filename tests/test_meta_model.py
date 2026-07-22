"""Test for the offline meta-model (spec 002, User Story 3).

Skipped entirely unless scikit-learn is installed, so the core suite stays
dependency-light. Asserts the trained report has the expected shape and one
coefficient per feature — not specific coefficient values.
"""

import json
import os

import pytest

pytest.importorskip("sklearn")  # analysis-only dep; skip when absent

import train_meta_model


def _synthetic_history(n=30):
    """Interleave pos/neg across increasing timestamps so the temporal split
    lands both classes on each side. pos → high scores, neg → low (+ noise)."""
    h = {}
    for i in range(n):
        is_pos = i % 2 == 0
        base = 0.7 if is_pos else 0.3
        jitter = (i % 5) * 0.01
        h[f"job{i}"] = {
            "status": "applied" if is_pos else "error",
            "error_class": "" if is_pos else "Escopo incorreto",
            "first_seen_at": f"2026-01-{i + 1:02d}T00:00:00",
            "score_gemini": int((base + jitter) * 100),
            "score_vetor_desc": round(base + jitter, 3),
            "score_title": round(base - 0.05 + jitter, 3),
        }
    return h


def test_meta_model_trains_and_reports(tmp_path, monkeypatch):
    hist_path = tmp_path / "hist.json"
    hist_path.write_text(json.dumps(_synthetic_history()), encoding="utf-8")
    monkeypatch.setenv("HISTORY_PATH", str(hist_path))
    monkeypatch.setenv("EVAL_MIN_LABELS", "10")

    model_path = tmp_path / "model.joblib"
    report_path = tmp_path / "report.json"
    rc = train_meta_model.main(["--model", str(model_path), "--report", str(report_path)])

    assert rc == 0
    assert model_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for key in ("trained_at", "n_train", "n_test", "features",
                "coefficients_standardized", "intercept", "test_auc", "brier",
                "reliability_bins", "note"):
        assert key in report, f"missing {key}"
    assert report["features"] == list(train_meta_model.FEATURES)
    assert set(report["coefficients_standardized"]) == set(train_meta_model.FEATURES)
    assert isinstance(report["reliability_bins"], list)


def test_meta_model_insufficient_labels_exits_zero(tmp_path, monkeypatch):
    hist_path = tmp_path / "hist.json"
    hist_path.write_text(json.dumps(_synthetic_history(4)), encoding="utf-8")
    monkeypatch.setenv("HISTORY_PATH", str(hist_path))
    monkeypatch.setenv("EVAL_MIN_LABELS", "20")

    rc = train_meta_model.main(["--model", str(tmp_path / "m.joblib"),
                                "--report", str(tmp_path / "r.json")])
    assert rc == 0
    assert not (tmp_path / "m.joblib").exists()  # nothing trained/written

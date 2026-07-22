"""Offline meta-model over the three scores (spec 002, User Story 3).

Fits an interpretable logistic regression on the user's labels to fuse
score_gemini + score_vetor_desc + score_title into a single calibrated
probability of applying. Its coefficients answer the standing question: does the
paid/quota-limited score_gemini add predictive value over the two free cosines?

Artifact only — NOT wired into the pipeline. Reads history READ-ONLY; writes only
the model + a human-readable report, both separate from the history file.

Requires the analysis extra:
    .venv/Scripts/python.exe -m pip install -r requirements-analysis.txt
Usage:
    .venv/Scripts/python.exe train_meta_model.py [--model PATH] [--report PATH]

Exit codes: 0 success (incl. insufficient data); 2 IO error; 3 scikit-learn missing.
"""
import argparse
import json
import os
import sys
from datetime import datetime

from eval_scores import auc, temporal_split
from src.match_labels import label_of
from src.settings import env_int, env_float, env_str

FEATURES = ("score_gemini", "score_vetor_desc", "score_title")


def _labeled_rows(history):
    """(features, is_pos, first_seen_at) for entries with a label AND all three
    scores present. Rows with a null score_gemini (local-fallback runs) are
    dropped so the LLM's contribution is measured only where it actually ran."""
    rows = []
    for entry in history.values():
        lab = label_of(entry)
        if lab is None:
            continue
        if any(entry.get(f) is None for f in FEATURES):
            continue
        rows.append(({f: float(entry[f]) for f in FEATURES},
                     lab == "pos", entry.get("first_seen_at", "")))
    return rows


def _reliability_bins(probs, ys, n_bins=5):
    bins = []
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        idx = [i for i, p in enumerate(probs) if (lo <= p < hi or (b == n_bins - 1 and p == 1.0))]
        if not idx:
            continue
        bins.append({"p_mean": round(sum(probs[i] for i in idx) / len(idx), 3),
                     "obs_rate": round(sum(ys[i] for i in idx) / len(idx), 3),
                     "n": len(idx)})
    return bins


def main(argv=None):
    parser = argparse.ArgumentParser(description="Train the match meta-model.")
    parser.add_argument("--model", help="model output path (default META_MODEL_PATH)")
    parser.add_argument("--report", help="report output path (default META_MODEL_REPORT_PATH)")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv()  # pick up HISTORY_PATH etc. from .env, like main.py does

    # Report/summary print non-ASCII (→); avoid a Windows cp1252 console crash.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    history_path = env_str("HISTORY_PATH", "vagas_historico.json")
    try:
        with open(history_path, encoding="utf-8") as f:  # read-only
            history = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Cannot read history '{history_path}': {e}", file=sys.stderr)
        return 2
    if not isinstance(history, dict):
        print(f"History '{history_path}' is not the expected object map.", file=sys.stderr)
        return 2

    rows = _labeled_rows(history)
    min_labels = env_int("EVAL_MIN_LABELS", 20)
    n_pos = sum(1 for _, y, _ in rows if y)
    if len(rows) < min_labels or n_pos == 0 or n_pos == len(rows):
        print(f"Insufficient labels for a meta-model (usable rows={len(rows)}, "
              f"positives={n_pos}, need ≥{min_labels} and both classes). "
              "Triage more jobs (inscrito / irrelevant / 'Escopo incorreto') and re-run.")
        return 0

    try:
        import joblib
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        print("scikit-learn is not installed. Run:\n"
              "  .venv/Scripts/python.exe -m pip install -r requirements-analysis.txt",
              file=sys.stderr)
        return 3

    train, test = temporal_split(
        [{"f": f, "y": y, "first_seen_at": t} for f, y, t in rows],
        env_float("TEMPORAL_SPLIT_FRAC", 0.7),
    )
    # Guard: temporal split can land all of one class on a side with small data.
    if not train or not test or len({r["y"] for r in train}) < 2:
        print("Temporal split left a side single-class; not enough spread to train. "
              "Add more labels across time and re-run.")
        return 0

    X_train = [[r["f"][k] for k in FEATURES] for r in train]
    y_train = [1 if r["y"] else 0 for r in train]
    X_test = [[r["f"][k] for k in FEATURES] for r in test]
    y_test = [1 if r["y"] else 0 for r in test]

    model = Pipeline([("scaler", StandardScaler()),
                      ("lr", LogisticRegression(max_iter=1000))])
    model.fit(X_train, y_train)

    probs = [float(p[1]) for p in model.predict_proba(X_test)]
    test_auc = auc(list(zip(probs, [bool(y) for y in y_test])))
    brier = sum((p - y) ** 2 for p, y in zip(probs, y_test)) / len(y_test)
    lr = model.named_steps["lr"]
    coefs = {k: round(float(c), 4) for k, c in zip(FEATURES, lr.coef_[0])}

    report = {
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "n_train": len(train), "n_test": len(test),
        "features": list(FEATURES),
        "coefficients_standardized": coefs,
        "intercept": round(float(lr.intercept_[0]), 4),
        "test_auc": None if test_auc is None else round(test_auc, 3),
        "brier": round(brier, 3),
        "reliability_bins": _reliability_bins(probs, y_test),
        "note": ("Coefficients are in standardized-feature space (comparable across scores). "
                 "Rows with a null score_gemini were dropped. A near-zero score_gemini "
                 "coefficient means the LLM adds little over the two free cosines."),
    }

    model_path = args.model or env_str("META_MODEL_PATH", "data/meta_model.joblib")
    report_path = args.report or env_str("META_MODEL_REPORT_PATH", "data/meta_model_report.json")
    try:
        os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
        joblib.dump(model, model_path)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"Failed to write artifacts: {e}", file=sys.stderr)
        return 2

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n(model → {model_path}, report → {report_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

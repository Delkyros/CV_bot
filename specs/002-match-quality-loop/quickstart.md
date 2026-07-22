# Quickstart: Validate the Match-Quality Loop

Prerequisites: the venv (`.venv/Scripts/python.exe`), a `vagas_historico.json` with some triaged
jobs (`inscrito` / `irrelevant` / `Escopo incorreto` marks). No new install needed for the harness or
few-shot; the meta-model needs the analysis extra.

## 1. Evaluation harness (US1 — the MVP)

```bash
.venv/Scripts/python.exe eval_scores.py
```

**Expect**: a printed report + a written `data/eval_report.md` containing, for each of
`score_gemini` / `score_vetor_desc` / `score_title`: AUC, a suggested threshold, pos-vs-neg medians;
a temporal (train/test) section; per-term and PT/EN slices with an inversion flag; and a golden-set
section. On first run it also creates `data/golden_set.json`.

**Validate**
- With few/no labels: the report says `insufficient labels (n=X, need Y)` rather than a number, and
  exits 0.
- Re-run: identical output (deterministic); the golden set is not re-created.
- `--refreeze`: `data/golden_set.json` is rebuilt from the current N most-recent labeled jobs.
- History is untouched: `git status` / mtime on `vagas_historico.json` unchanged after a run.

## 2. Few-shot exemplars in the LLM prompt (US2 — the only live-path change)

Enabled by default (`FEWSHOT_ENABLED=true`). With ≥ `FEWSHOT_MIN_LABELS` labels, a pipeline run's
match prompt includes your own accept/reject examples.

**Validate (no LLM spend needed)** — a unit test asserts:
- `_build_prompt(job, profile, exemplars=None)` is **byte-identical** to the pre-feature prompt.
- With exemplars, the block appears, stays within `FEWSHOT_CHAR_BUDGET`, and the JSON response
  contract is unchanged.
- With labels below `FEWSHOT_MIN_LABELS`, or `FEWSHOT_ENABLED=false`, the prompt falls back to the
  exemplar-free form.
- Forcing an exception in selection → run proceeds with the fallback prompt (Principle VI).

To see it live: run the pipeline (`.venv/Scripts/python.exe main.py`) and confirm it completes and
scores as before — behavior is unchanged except score alignment.

## 3. Meta-model (US3 — offline, interpretable)

```bash
.venv/Scripts/python.exe -m pip install -r requirements-analysis.txt
.venv/Scripts/python.exe train_meta_model.py
```

**Expect**: `data/meta_model.joblib` + `data/meta_model_report.json` with per-feature coefficients,
intercept, temporal-test AUC, Brier score, and reliability bins.

**Validate**
- Read `coefficients.score_gemini` vs the two cosines: a near-zero LLM coefficient is evidence the
  LLM adds little over the free signals (answers "is the LLM worth its cost?", SC-003).
- Below `EVAL_MIN_LABELS`: training is skipped with an "insufficient labels" message, exit 0.
- Without `scikit-learn` installed: exits 3 with the pip hint; the pipeline and harness are
  unaffected.
- Pipeline runtime unchanged: the artifact exists but nothing loads it during `main.py`.

## Regression gate (across future changes)

After changing a model, threshold, or the few-shot prompt, re-run `eval_scores.py` and compare the
**golden-set** section to the previous report. A drop there is a regression to investigate before
adopting the change.

## Test suite

```bash
.venv/Scripts/python.exe -m pytest tests/test_match_labels.py tests/test_fewshot_prompt.py -q
# meta-model test runs only if scikit-learn is installed (otherwise skipped):
.venv/Scripts/python.exe -m pytest tests/test_meta_model.py -q
```

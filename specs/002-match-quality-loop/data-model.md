# Phase 1 Data Model: Match-Quality Continuous-Improvement Loop

No database. Everything is derived from the existing `vagas_historico.json` (read-only) plus two
new small files. Entities below are logical shapes, not new history fields.

## Existing inputs (read, never written)

### History entry (subset used)

From `vagas_historico.json` (keyed by `job_link`). Fields this feature consumes:

| Field | Type | Use |
|-------|------|-----|
| `job_title` | str | nearest-term slice, exemplar text |
| `company` | str | exemplar text |
| `full_description` / `verdict`/`strengths`/`gaps` | str/list | exemplar text (see note) |
| `location` | str | (available; not required) |
| `score_gemini` | int \| null | feature / evaluated score |
| `score_vetor_desc` | float 0..1 | feature / evaluated score |
| `score_title` | float 0..1 | feature / evaluated score |
| `status` | str | label derivation |
| `error_class` | str | label derivation |
| `first_seen_at` | ISO-8601 str | temporal split, recency ordering |

> Note: history stores analysis prose (`strengths`/`gaps`/`verdict`) but may not retain the full raw
> description. Exemplars use whatever job text is present (title + company + verdict/gaps), truncated
> to the char budget — enough to convey why a job was accepted/rejected.

## Derived entity: Label

Computed at read time by `src/match_labels.py::label_of(entry)`. **Not stored.**

| Value | Condition |
|-------|-----------|
| `"pos"` | `status == "applied"` |
| `"neg"` | `error_class == "Escopo incorreto"` |
| `None` (excluded) | `viewed`, `new`, `irrelevant`, `duplicado`, untriaged, or `error_class` ∈ {`Localidade/Modelo incorreto`, `Não é CLT`} |

Persisted `status` values are English (`new`/`viewed`/`applied`/`error`, per `webapp.py`); the UI
labels are Portuguese. Precedence: a `Escopo incorreto` scope error makes the job negative even if
`status == applied` (explicit "wrong role"). `irrelevant` is auto-assigned by the pipeline for sub-bar
jobs, so it is **excluded** rather than treated as a user negative (avoids circular evaluation).

## Derived entity: Slice key

Per job, for the per-slice report:

- **nearest_term**: `argmax(term in termos_busca) similarity(job_title_head, term)` via `local_match`.
- **language**: `"pt" | "en"` from `is_probably_pt(text)` heuristic.

## New file: Golden-set snapshot

Path `GOLDEN_SET_PATH` (default `data/golden_set.json`), separate from history. Written once (or on
`--refreeze`), read-only thereafter.

```json
{
  "created_at": "2026-07-22T12:00:00",
  "size": 50,
  "criteria": "N most-recent labeled jobs by first_seen_at",
  "items": [
    {
      "job_link": "https://…",
      "label": "pos",
      "score_gemini": 82,
      "score_vetor_desc": 0.47,
      "score_title": 0.63,
      "first_seen_at": "2026-06-30T09:12:00"
    }
  ]
}
```

Evaluation reads `label` + scores from here (frozen), never from live history.

## New file: Meta-model artifact + report

- `META_MODEL_PATH` (default `data/meta_model.joblib`) — the pickled sklearn pipeline
  (scaler + `LogisticRegression` + calibration). Written by `train_meta_model.py`.
- `META_MODEL_REPORT_PATH` (default `data/meta_model_report.json`) — human-readable:

```json
{
  "trained_at": "2026-07-22T12:00:00",
  "n_train": 120, "n_test": 40,
  "features": ["score_gemini", "score_vetor_desc", "score_title"],
  "coefficients": {"score_gemini": 0.21, "score_vetor_desc": 1.34, "score_title": 0.58},
  "intercept": -2.1,
  "test_auc": 0.78,
  "brier": 0.17,
  "reliability_bins": [{"p_mean": 0.1, "obs_rate": 0.08, "n": 12}, "…"],
  "note": "score_gemini coefficient near zero → LLM adds little over the free cosines"
}
```

Missing-feature handling: rows where `score_gemini` is null (local-fallback runs) are either dropped
from training or imputed to the median — decided in the script and stated in the report `note`;
default is to drop, so the LLM's contribution is measured only where it actually ran.

## Derived entity: Evaluation report (harness output)

Printed and written to `EVAL_REPORT_PATH` (default `data/eval_report.md`). Sections:

1. **Overall**: n positives / negatives / excluded; per-score {AUC, suggested threshold, pos vs neg
   median}.
2. **Temporal**: same per-score metrics on the recent test split; report **both** train and test AUC
   and flag when `test AUC < train AUC − 0.05` (a concrete drift signal, no vague "materially").
3. **Slices**: per nearest-term and per language — per-score AUC; **inversion flag** when negatives
   outscore positives (AUC < 0.5).
4. **Golden set**: current per-score AUC/threshold-hit on the frozen snapshot (the regression
   baseline).
5. **Insufficient-data notes**: any overall/slice/split below `EVAL_MIN_LABELS` (or single-class)
   reported as `insufficient labels (n=X, need Y)` instead of a number.

## New tunables (all via `src/settings.py`, env with defaults)

| Env var | Default | Meaning |
|---------|---------|---------|
| `EVAL_MIN_LABELS` | 20 | below this (overall or per slice) → "insufficient", no metric |
| `TEMPORAL_SPLIT_FRAC` | 0.7 | oldest fraction = train, rest = test |
| `GOLDEN_SET_SIZE` | 50 | N most-recent labeled jobs frozen |
| `GOLDEN_SET_PATH` | `data/golden_set.json` | frozen snapshot location |
| `EVAL_REPORT_PATH` | `data/eval_report.md` | harness report output |
| `FEWSHOT_ENABLED` | `true` | master switch; false → exact current prompt |
| `FEWSHOT_MIN_LABELS` | 10 | below this → no exemplars (fallback prompt) |
| `FEWSHOT_MAX_EXEMPLARS` | 3 | per class (accept / reject) |
| `FEWSHOT_CHAR_BUDGET` | 1500 | total chars for the exemplar block; truncate to fit |
| `META_MODEL_PATH` | `data/meta_model.joblib` | saved model |
| `META_MODEL_REPORT_PATH` | `data/meta_model_report.json` | coefficients/metrics |

Defaults are starting points; the whole point of the harness is to tune `EVAL_MIN_LABELS` /
thresholds against real data. Values are illustrative and finalized during implementation against the
current history volume.

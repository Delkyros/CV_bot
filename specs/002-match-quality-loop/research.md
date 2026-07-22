# Phase 0 Research: Match-Quality Continuous-Improvement Loop

All decisions favor the laziest option that is correct on edge cases, per the project's
default-keep / never-mislead posture. No open NEEDS CLARIFICATION remain.

## D1 — Dependency for the meta-model (logistic regression + calibration)

**Decision**: Add `scikit-learn` as an **analysis-only** dependency in a new
`requirements-analysis.txt` (`-r requirements.txt` + `scikit-learn`). The runtime (`main.py`
pipeline, `webapp.py`) MUST NOT import it; only `train_meta_model.py` does.

**Rationale**: A calibrated, regularized logistic regression with clean per-feature coefficients is
the exact thing `sklearn.linear_model.LogisticRegression` (+ `CalibratedClassifierCV`) does. Hand-
rolling gradient descent + calibration is error-prone and is *not* "a few lines done right" — the
one case where ponytail says take the library. Being offline-only, it adds zero runtime weight and
zero risk to the pipeline/Docker image (which need not install it).

**Alternatives rejected**:
- Hand-rolled LR on numpy: reinvents a stdlib-of-ML, no calibration, easy to get regularization
  wrong on tiny data.
- Put sklearn in `requirements.txt`: pollutes the runtime image with numpy/scipy for a tool the
  pipeline never runs. Rejected.

## D2 — AUC and thresholds in the harness (keep P1 dependency-free)

**Decision**: Compute AUC in **pure Python** via the rank-based Mann–Whitney identity
(AUC = (sum of positive ranks − n_pos·(n_pos+1)/2) / (n_pos·n_neg)); suggest a threshold via the
point maximizing Youden's J (tpr − fpr) over the observed score values. No numpy, no sklearn.

**Rationale**: The harness is the P1 MVP and should run with the current environment (no new deps).
Rank-AUC is ~10 lines and exact; Youden's J over a few hundred points is trivial. Keeps the harness
usable the day it lands, before the meta-model's sklearn is installed.

**Alternatives rejected**: `sklearn.metrics.roc_auc_score` — correct but would couple the P1 harness
to the analysis-only dependency. Deferred to the meta-model, which already needs sklearn.

## D3 — Ground-truth labels (single source of truth)

**Decision**: One helper module `src/match_labels.py` exposes `label_of(entry) -> "pos"|"neg"|None`:
positive iff `status == "inscrito"`; negative iff `status == "irrelevant"` OR
`error_class == "Escopo incorreto"`; everything else (`visto`, `new`, `duplicado`, untriaged, and
`error_class` in {`Localidade/Modelo incorreto`, `Não é CLT`}) → `None` (excluded). Harness,
meta-model, and exemplar selector all call it.

**Rationale**: The positive/negative definition is load-bearing (Clarifications). Defining it in one
place prevents the three consumers from drifting apart. Matches spec FR-002 exactly.

**Alternatives rejected**: Re-deriving labels in each script — guarantees eventual divergence.

## D4 — Temporal validation split

**Decision**: Sort labeled entries by `first_seen_at` (already stored, ISO-8601, sortable). Train
portion = oldest `TEMPORAL_SPLIT_FRAC` (default 0.7); test portion = most recent remainder. Derive
thresholds on train, report AUC/threshold-hit-rate on test. Never a random shuffle.

**Rationale**: `first_seen_at` is present on every entry — no new field, no backfill. A fractional
split by time directly models "does a threshold tuned on the past still hold on recent jobs?" (spec
FR-003, market-drift concern).

**Alternatives rejected**: Random k-fold (ignores drift, spec forbids); fixed calendar cutoff
(brittle with sparse months). Fraction split adapts to whatever volume exists.

## D5 — Per-search-term slice without a stored term

**Decision**: History entries do **not** record which `termo_busca` found the job. Approximate the
slice by assigning each job to its **nearest search term** = argmax over `termos_busca` of
`local_match` title↔term cosine (the same signal behind `score_title`, but argmax instead of max).
Load `termos_busca` from `config/keywords.yaml`. Mark the approximation with a `ponytail:` comment.

**Rationale**: Zero schema change, zero backfill, works uniformly on all existing history. Reuses the
already-loaded model2vec model. The nearest-term is exactly the axis `score_title` measures, so the
slice is meaningful for "is this score inverting for some role?".

**Alternatives rejected**:
- Add `matched_term` at collection + backfill: touches the collect/save path (Principle III care) and
  can't recover the true term for existing entries anyway. Deferred — noted as a future improvement if
  the approximation proves too coarse.

## D6 — PT vs EN slice

**Decision**: A tiny pure-Python heuristic `is_probably_pt(text)` — presence of Portuguese diacritics
(`ã õ ç` etc.) and/or common PT stopwords (`de, para, com, vaga, experiência`) above a small count →
PT, else EN. Used only to bucket the slice. `ponytail:` comment names the ceiling (a real language
detector if the heuristic misclassifies).

**Rationale**: Slicing only needs a coarse bucket, not a classifier. Adding `langdetect`/`fasttext`
for a two-way split on job text is overkill. The heuristic is a dozen lines and inspectable.

**Alternatives rejected**: `langdetect` (new dep, model download, overkill for 2 buckets).

## D7 — Golden-set snapshot storage

**Decision**: On first run needing it, the harness writes the **N most-recent labeled jobs** (by
`first_seen_at`, `GOLDEN_SET_SIZE` default e.g. 50) with `{job_link, label, score_gemini,
score_vetor_desc, score_title, first_seen_at}` to `GOLDEN_SET_PATH` (default under `data/`,
separate from history). Subsequent runs read the frozen file; labels come from the snapshot, never
live history. A `--refreeze` flag re-creates it deliberately.

**Rationale**: Freezing labels + scores makes the gate reproducible and immune to re-triage drift
(spec FR-005, edge case "golden set drift"). Separate file honors Principle III. N most-recent keeps
it automatic (no manual curation) and representative of current criteria.

**Alternatives rejected**: Manual curation (maintenance burden), random sample (needs a stored seed to
reproduce). Both add work for no gain at this scale — chosen per clarification Q2.

## D8 — Few-shot injection point and safety

**Decision**: `main.py` builds the exemplar set **once per run** (it already loads history for the
scope blocklist) via `match_labels`: take up to `FEWSHOT_MAX_EXEMPLARS` (default small, e.g. 3+3) most
recent positives and negatives, truncate each job's text to fit `FEWSHOT_CHAR_BUDGET`. Pass the
prepared block to `analyze_and_filter_jobs` → `analyze_match(job, profile, exemplars=block)` →
`_build_prompt`, which inserts an "examples of the candidate's own accept/reject decisions" section
before the analysis instructions. Empty/None block → the current prompt verbatim. The whole selection
is wrapped in try/except (Principle VI): any error logs and yields `None` → exemplar-free prompt.
`FEWSHOT_ENABLED` (default true) and `FEWSHOT_MIN_LABELS` gate activation; below the minimum → no
exemplars.

**Rationale**: Selecting once per run (not per job) is cheap and keeps exemplars stable within a run.
Threading an optional argument keeps `analyze_match` backward-compatible and the fallback trivial. The
response contract is unchanged — only the prompt grows (spec FR-008/009/010/011).

**Alternatives rejected**:
- `matcher` reads history itself: duplicates history-loading, couples the matcher to file paths, and
  re-reads per job. Rejected — `main` already has the data.
- Unbounded exemplars / full descriptions: token blow-up and cost. Rejected — bounded count + char
  budget with truncation.

## D9 — Reliability/calibration metric for the meta-model

**Decision**: Report the meta-model's AUC on the temporal test split (comparable to the individual
scores) plus a coarse calibration summary (Brier score, and a 5-bin reliability table). Report each
feature's coefficient and the intercept so the user can read whether `score_gemini` carries weight
beyond the two cosines.

**Rationale**: Brier + a small reliability table are enough to see "is P(apply) calibrated" without a
plotting stack. Coefficients directly answer the standing "is the LLM worth its cost?" question
(spec FR-013, SC-003).

**Alternatives rejected**: Calibration plots / reliability diagrams as images — needs matplotlib;
a text table conveys the same at this scale.

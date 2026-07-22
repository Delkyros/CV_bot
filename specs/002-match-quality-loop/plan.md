# Implementation Plan: Match-Quality Continuous-Improvement Loop

**Branch**: `002-match-quality-loop` | **Date**: 2026-07-22 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/002-match-quality-loop/spec.md`

## Summary

Close the feedback loop using labels the user already produces. Three pieces, two of them
purely offline:

1. **Evaluation harness** (`eval_scores.py`, new top-level script) — reads
   `vagas_historico.json` read-only, derives labels from `status`/`error_class`, and reports
   per-score separation (AUC), suggested thresholds, a temporal (train-on-past/test-on-recent)
   split, per-slice breakdowns (nearest search term, PT vs EN), and a frozen golden-set
   regression gate. Prints + writes a report file. Stdlib-only (AUC is rank-based); reuses the
   existing model2vec embeddings only for the nearest-term slice.
2. **Few-shot from the user's decisions** — the only live-path change. `main.py` selects a
   bounded set of accept (`inscrito`) / reject (`Escopo incorreto`/`irrelevant`) exemplars from
   history once per run and passes them to `matcher.analyze_match`, which injects them into
   `_build_prompt`. Best-effort: any failure or too-few-labels falls back to today's exemplar-free
   prompt, byte-identical. No drop/keep decision changes.
3. **Meta-model** (`train_meta_model.py`, new offline script) — fits an interpretable logistic
   regression on the three scores → a calibrated P(apply), saves the model + a human-readable
   coefficients/metrics JSON. Evaluated through the harness. **Not wired into the pipeline.**
   This is the only piece that needs `scikit-learn`, added as an **analysis-only** dependency the
   runtime never imports.

Outcome tracking (former US4) was cut during clarification.

## Technical Context

**Language/Version**: Python 3.12 (venv `.venv/Scripts/python.exe`).

**Primary Dependencies**:
- Harness + few-shot: **Python stdlib only** (`json`, `math`, `statistics`, `datetime`,
  `argparse`, `os`). Nearest-term slicing reuses the already-present `chonkie[model2vec]`
  embeddings via `src/local_match.py`.
- Meta-model: **`scikit-learn`** (pulls `numpy`/`scipy`) — NEW, **analysis-only**. Isolated in a
  new `requirements-analysis.txt`; neither `main.py`/pipeline nor `webapp.py` imports it.

**Storage**: JSON files. Reads existing `vagas_historico.json`. NEW files, both separate from the
history and env-pathed: a frozen **golden-set snapshot** and the **meta-model artifact** (+ its
coefficients/metrics JSON). The report file is a plain text/markdown output. No piece writes the
history (preserves Principle III).

**Testing**: pytest (`.venv/Scripts/python.exe -m pytest -q`). New unit tests for label derivation,
AUC, temporal split, exemplar selection/fallback. Meta-model test gated on `scikit-learn` presence
(skip if absent) so the core suite stays dependency-light.

**Target Platform**: Local (Windows/Linux); the offline scripts are run by hand. The few-shot change
runs wherever the pipeline runs (local + Docker).

**Project Type**: Single project — batch pipeline (`main.py`) + Flask UI (`webapp.py`) + shared
`src/`, plus new top-level analysis scripts.

**Performance Goals**: Harness runs over the whole history in seconds (embeddings for the nearest-term
slice are the only cost; term vectors are `lru_cache`d as today). No latency budget — offline, manual.

**Constraints**: Few-shot must respect a token/char budget and never crash a run (Principle VI). No
new runtime dependency (sklearn is offline-only). All tunables env-configurable (Principle IV). No
silent job drops (Principles I & II).

**Scale/Scope**: Single user, small label volume (tens–hundreds). Every metric degrades gracefully
and reports "insufficient labels" below a configurable minimum (FR-019).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Impact | Verdict |
|-----------|--------|---------|
| I. Default-CLT | Classification untouched. | ✅ Pass |
| II. Default-In-Scope | Few-shot only enriches the LLM prompt; no threshold, gate, or `core_role_compatible` handling changes (FR-011). No new drop path. | ✅ Pass |
| III. Merge-Don't-Overwrite State | Harness/meta-model are read-only over history; new artifacts (golden set, model) are SEPARATE env-pathed files. No piece writes `vagas_historico.json`. | ✅ Pass |
| IV. Everything Env-Configurable | All new tunables (min-labels, exemplar count/char budget, golden-set N + path, temporal split fraction, model path) via `src/settings.py` helpers with defaults (FR-017). | ✅ Pass |
| V. Portuguese Config, English Code | New code/env English; no new user-facing UI (US4 cut). Exemplars quote existing PT job text — data, not new labels. | ✅ Pass |
| VI. Never-Crash Resilience | Exemplar selection/injection is best-effort inside a try/except → falls back to today's prompt; a match never fails because of it. Offline scripts are separate processes, cannot crash the pipeline. | ✅ Pass |
| VII. Self-Adjusting Scope Filter | Untouched (few-shot targets match score, not the scope blocklist). | ✅ Pass |

**Result**: No violations. Complexity Tracking not required.

**Post-Phase-1 re-check**: Still no violations — the design adds no dependency to the runtime, adds no
history writer, and the one live-path touch (few-shot) is guarded and reversible via env
(`FEWSHOT_ENABLED=false` → exact current behavior).

## Project Structure

### Documentation (this feature)

```text
specs/002-match-quality-loop/
├── plan.md              # This file
├── research.md          # Phase 0 — dep isolation, AUC, temporal split, term/lang slicing
├── data-model.md        # Phase 1 — label derivation, report shape, golden-set + model files
├── quickstart.md        # Phase 1 — how to validate each of the 3 pieces
├── contracts/
│   ├── eval-cli.md      # eval_scores.py + train_meta_model.py CLI + report contract
│   └── artifacts.md     # golden-set file, meta-model artifact, few-shot prompt-block contract
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
eval_scores.py           # NEW: offline harness — label derivation, AUC, thresholds, temporal
│                        #      split, slices, golden-set gate; prints + writes report. stdlib.
train_meta_model.py      # NEW: offline — fit logistic regression on 3 scores, save model +
│                        #      coefficients/metrics JSON. scikit-learn (analysis-only).
main.py                  # CHANGE: select exemplars from history once per run (best-effort),
│                        #         pass to analyze_and_filter_jobs → analyze_match.
src/
├── matcher.py           # CHANGE: analyze_match(job, profile, exemplars=None); _build_prompt
│                        #         gains an optional exemplar block (empty → current prompt).
├── match_labels.py      # NEW: shared label derivation + slicing helpers (used by harness,
│                        #      meta-model, and exemplar selection — one source of truth).
├── local_match.py       # REUSE: nearest-term similarity for the per-term slice (no change,
│                        #        or a tiny argmax helper alongside title_scope_similarity).
└── settings.py          # REUSE: env helpers for the new tunables.
requirements-analysis.txt # NEW: `-r requirements.txt` + scikit-learn (offline meta-model only).
tests/
├── test_match_labels.py     # NEW: label derivation, temporal split, AUC, slicing edge cases.
├── test_fewshot_prompt.py   # NEW: exemplar selection bound/budget + graceful fallback.
└── test_meta_model.py       # NEW (skip if sklearn absent): fit + coefficient report shape.
```

**Structure Decision**: Single project. Label logic lives once in `src/match_labels.py` so the
harness, the meta-model, and the exemplar selector all derive positives/negatives identically (the
spec's load-bearing definition). The two offline scripts are top-level (like `main.py`/`webapp.py`),
run by hand. The only runtime edit is threading an optional `exemplars` argument through
`main.py` → `matcher.analyze_match` → `_build_prompt`, guarded so absence == today.

## Complexity Tracking

> No constitution violations — section intentionally empty.

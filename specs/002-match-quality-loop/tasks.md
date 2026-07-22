---
description: "Task list for Match-Quality Continuous-Improvement Loop"
---

# Tasks: Match-Quality Continuous-Improvement Loop

**Input**: Design documents from `specs/002-match-quality-loop/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Included. The plan names the test files and Constitution §Development Workflow requires
filter-touching changes to add cases. Per project convention (CLAUDE.md), tests are **written here but
NOT executed automatically** — the user runs `pytest` when they choose.

**Organization**: Tasks grouped by user story (US1 harness → US2 few-shot → US3 meta-model), each
independently implementable and testable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no incomplete-task dependency)
- **[Story]**: US1 / US2 / US3 (Setup, Foundational, Polish have no story label)

## Path Conventions

Single project: new top-level scripts (`eval_scores.py`, `train_meta_model.py`), shared logic in
`src/`, tests in `tests/`. Paths from [plan.md](plan.md#source-code-repository-root).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Declare the one new (analysis-only) dependency. No runtime setup needed — the harness and
few-shot use stdlib + existing deps.

- [X] T001 [P] Create `requirements-analysis.txt` at repo root: `-r requirements.txt` + `scikit-learn`. Add a header comment that this is **offline/analysis-only** and the pipeline/webapp never import it. (Consumed only by US3.)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The single load-bearing shared piece — the ground-truth label definition that US1, US2,
and US3 must all agree on (spec FR-002, Clarifications Q1).

**⚠️ CRITICAL**: No user story can begin until `label_of` exists — all three derive labels from it.

- [X] T002 Create `src/match_labels.py` with `label_of(entry) -> "pos" | "neg" | None`: positive iff `status == "inscrito"`; negative iff `status == "irrelevant"` OR `error_class == "Escopo incorreto"`; everything else (`visto`, `new`, `duplicado`, untriaged, `error_class` ∈ {`Localidade/Modelo incorreto`, `Não é CLT`}) → `None`. Conflict `inscrito` + `Escopo incorreto` resolves to `neg`. English code, module docstring citing FR-002.
- [X] T003 [P] Create `tests/test_match_labels.py` with `label_of` cases: each positive/negative/excluded branch, the conflict-precedence case, and empty/missing-field entries.

**Checkpoint**: Label definition is single-sourced; user stories can proceed independently.

---

## Phase 3: User Story 1 - Evaluation harness (Priority: P1) 🎯 MVP

**Goal**: One offline command reports how well each of the three scores separates the user's accepted
from rejected jobs — overall, temporally, per slice, and against a frozen golden set. Stdlib-only.

**Independent Test**: Run `.venv/Scripts/python.exe eval_scores.py` on the current history → a printed
+ written report per [quickstart.md](quickstart.md#1-evaluation-harness-us1--the-mvp); on thin data it
prints "insufficient labels" and exits 0; history file is left untouched.

- [X] T004 [P] [US1] Add `nearest_term(title, search_terms)` (argmax of `local_match` title↔term cosine) and `is_probably_pt(text)` (diacritic/stopword heuristic) to `src/match_labels.py`. `ponytail:` comment on the nearest-term approximation (term not stored in history) and its upgrade path (research D5/D6).
- [X] T005 [P] [US1] In `eval_scores.py`, implement pure-Python `auc(scores, labels)` (rank-based Mann–Whitney) and `suggest_threshold(scores, labels)` (Youden's J), plus a `single_class`/empty guard returning a "not computable" marker (research D2).
- [X] T006 [US1] In `eval_scores.py`, implement `temporal_split(entries, frac=TEMPORAL_SPLIT_FRAC)` sorting by `first_seen_at` (oldest `frac` = train, recent remainder = test) (research D4).
- [X] T007 [US1] In `eval_scores.py`, implement golden-set create/read with `--refreeze`: snapshot the `GOLDEN_SET_SIZE` most-recent labeled jobs (`{job_link, label, scores, first_seen_at}`) to `GOLDEN_SET_PATH`; evaluation reads labels from the snapshot, never live history (data-model + contracts/artifacts.md). If fewer than `GOLDEN_SET_SIZE` labeled jobs exist, freeze whatever exists and record the actual count in the file's `size` field.
- [X] T008 [US1] In `eval_scores.py`, implement `render_report(...)`: overall per-score {AUC, threshold, pos/neg medians}, temporal section, per-slice (nearest_term, PT/EN) with inversion flag (AUC < 0.5), golden-set section, and per-scope `insufficient labels (n=X, need Y)` gating on `EVAL_MIN_LABELS` (data-model report shape).
- [X] T009 [US1] Wire `eval_scores.py` `main()`/argparse (`--refreeze`, `--report PATH`, `--quiet`): load history **read-only** (existing history path env) and `termos_busca` from `config/keywords.yaml`; print + write `EVAL_REPORT_PATH`; exit `0` (incl. insufficient-data) / `2` (bad args or unreadable history) per contracts/eval-cli.md. Read all tunables via `src/settings.py` env helpers with defaults (data-model tunables table).
- [X] T010 [US1] Extend `tests/test_match_labels.py` with: `nearest_term` + `is_probably_pt` on PT/EN samples, `auc` on a known-ordering fixture, `single_class`/empty guard, and `temporal_split` boundaries. (Same file as T003 → sequential.)

**Checkpoint**: The harness runs standalone and is the shippable MVP.

---

## Phase 4: User Story 2 - Few-shot from the user's decisions (Priority: P2)

**Goal**: The LLM match prompt carries the user's own accept/reject exemplars so `match_score` aligns
with their taste — bounded, budget-capped, and byte-identical to today when disabled or under-labeled.
This is the ONLY live-path change (spec FR-008..011, Principle VI).

**Independent Test**: `test_fewshot_prompt.py` proves `_build_prompt(..., exemplars=None)` equals the
pre-feature prompt, the block stays within budget when present, and disabled/too-few/exception all fall
back to the exemplar-free prompt.

- [X] T011 [US2] In `src/matcher.py`, add optional `exemplars=None` to `_build_prompt(job_info, candidate_profile, exemplars=None)`: falsy → return the **byte-identical** current prompt; present → insert the accept/reject calibration section before "Analysis Instructions" per contracts/artifacts.md. Response contract and `_parse_result` untouched.
- [X] T012 [US2] In `src/matcher.py`, add `exemplars=None` to `analyze_match(job_info, candidate_profile, exemplars=None)` and pass it to `_build_prompt`.
- [X] T013 [US2] In `main.py`, build the exemplar block **once per run** from the already-loaded history via `match_labels.label_of`: up to `FEWSHOT_MAX_EXEMPLARS` most-recent positives and negatives, rendered from stored title/company/verdict/gaps, truncated to `FEWSHOT_CHAR_BUDGET`; gate on `FEWSHOT_ENABLED` + `FEWSHOT_MIN_LABELS`; wrap in try/except → `None` on any failure (Principle VI). Thread it through `analyze_and_filter_jobs(...)` → `analyze_match`.
- [X] T014 [P] [US2] Create `tests/test_fewshot_prompt.py`: (a) `exemplars=None` byte-identical to a captured baseline prompt; (b) with exemplars, block present and total prompt ≤ budget; (c) `FEWSHOT_ENABLED=false` and labels < `FEWSHOT_MIN_LABELS` → fallback; (d) selection raising → fallback (no exception escapes); (e) **regression (Constitution §Dev Workflow)**: `analyze_and_filter_jobs` returns the identical set of kept jobs (drop/keep unchanged) with few-shot on vs off — few-shot only affects `score_gemini`, never a filter decision. Existing `tests/test_pipeline.py` and `tests/test_scope_filtering.py` MUST stay green.

**Checkpoint**: US1 + US2 both work; a pipeline run behaves as before except score alignment.

---

## Phase 5: User Story 3 - Meta-model fusing the three scores (Priority: P3)

**Goal**: An offline, interpretable model combines the three scores into a calibrated P(apply); its
coefficients answer "is `score_gemini` worth its cost?". Artifact only — NOT wired into the pipeline.

**Independent Test**: `train_meta_model.py` produces `meta_model.joblib` + a report JSON with
per-feature coefficients, temporal-test AUC, Brier, reliability bins; below `EVAL_MIN_LABELS` it skips
with a message; without sklearn it exits 3; the pipeline runtime is unchanged.

- [X] T015 [US3] Create `train_meta_model.py`: load history **read-only**, derive labels via `match_labels.label_of`, build the 3-feature matrix, **drop rows with null `score_gemini`** (local-fallback runs) and record that in the report note; `temporal_split` for train/test. Guard `EVAL_MIN_LABELS` → skip + exit 0.
- [X] T016 [US3] In `train_meta_model.py`, fit an sklearn `Pipeline` (StandardScaler + `LogisticRegression` + calibration), save to `META_MODEL_PATH` via joblib. Import sklearn lazily; ImportError → print the `pip install -r requirements-analysis.txt` hint and exit 3.
- [X] T017 [US3] In `train_meta_model.py`, write `META_MODEL_REPORT_PATH` JSON: per-feature coefficients, intercept, test AUC, Brier, 5-bin reliability table, `n_train`/`n_test`, and the drop/impute note (data-model). Add argparse `--model`/`--report`; exit `2` on IO error.
- [X] T018 [P] [US3] Create `tests/test_meta_model.py` guarded by `pytest.importorskip("sklearn")`: fit on a small synthetic labeled set, assert the report JSON has all keys and one coefficient per feature.

**Checkpoint**: All three stories independently functional.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Documentation and config surface for the new tunables (Principle IV mandates README
Tunables entries before use).

- [X] T019 [P] Add all new env tunables (data-model.md table) to the README Tunables table, and add one line to CLAUDE.md noting `eval_scores.py`/`train_meta_model.py` as the offline match-quality loop.
- [X] T020 [P] Add the new env vars with their defaults + short comments to `.env.example`.
- [X] T021 Validate per [quickstart.md](quickstart.md): run `eval_scores.py`, a pipeline run with few-shot, and (with the analysis extra) `train_meta_model.py`; confirm history mtime unchanged. Run the pytest suite **only if the user approves** (project convention: tests are not auto-run).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (T001)**: none — can start immediately (and is only needed by US3).
- **Foundational (T002–T003)**: blocks ALL user stories (`label_of` is shared).
- **US1 / US2 / US3**: each depends only on Foundational; mutually independent (any order, or parallel).
- **Polish (T019–T021)**: after the stories it documents are done.

### Within Each Story

- **US1**: T004 & T005 [P] (different files) → T006 → T007 → T008 → T009; T010 after T003 (same test file).
- **US2**: T011 → T012 (same file `matcher.py`) → T013 (`main.py`, calls `analyze_match`); T014 [P] anytime.
- **US3**: T015 → T016 → T017 (same file `train_meta_model.py`); T018 [P] anytime.

### Parallel Opportunities

- T001 and Foundational T003 are [P] against each other.
- After Foundational: **US1, US2, US3 can proceed fully in parallel** (disjoint files: `eval_scores.py`
  vs `matcher.py`+`main.py` vs `train_meta_model.py`; only `src/match_labels.py` T004 touches shared
  code, and only US1 does).
- Within a story, [P]-marked tasks (T004/T005, T014, T018) run in parallel.

---

## Parallel Example: User Story 1

```bash
# T004 and T005 touch different files → parallel:
Task: "Add nearest_term + is_probably_pt to src/match_labels.py"
Task: "Implement auc + suggest_threshold in eval_scores.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. T001? (skip — US3-only) → **T002–T003 Foundational** → **T004–T010 US1**.
2. **STOP and VALIDATE**: run `eval_scores.py` on real history; read the separation report. This alone
   answers "which score predicts my decisions?" — the whole point of the loop.

### Incremental Delivery

1. Foundational → US1 (harness, MVP) → validate.
2. Add US2 (few-shot) → the score that actually ranks starts learning from your taste.
3. Add US3 (meta-model) → coefficients settle the "is the LLM worth it?" question.
   Each story ships value without touching the others.

---

## Notes

- `[P]` = different files, no incomplete-task dependency.
- Only US2 changes runtime behavior; `FEWSHOT_ENABLED=false` reverts it exactly.
- No task writes `vagas_historico.json` (Principle III); new artifacts are separate env-pathed files.
- Per CLAUDE.md, do NOT run the test suite automatically — author tests, let the user execute.
- **Principle IV ("document before use")**: add each new tunable to the README Tunables table and
  `.env.example` in the same commit as the story that introduces it; T019/T020 are the
  consolidation/backstop, not the first time a var is documented.
- **A1 (analyze) — consciously not fixed**: FR-011/FR-018/FR-019 partly restate Principles II/VI and
  SC-006 for per-task traceability. Deleting/merging them would renumber every FR (churn + breaks the
  coverage table) for a cosmetic dedup — not worth it. Left intentionally.
- Commit after each task or logical group.

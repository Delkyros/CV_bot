# Feature Specification: Match-Quality Continuous-Improvement Loop

**Feature Branch**: `002-match-quality-loop`

**Created**: 2026-07-22

**Status**: Draft

**Input**: User description: "Continuous-improvement loop for job-match quality, using the labels the user already produces in the web app as ground truth. Three high-impact, low-cost pieces: an offline evaluation harness (`eval_scores.py`), a meta-model that fuses the three scores into one calibrated probability, and few-shot from the user's own accept/reject decisions injected into the Gemini prompt. Manual/offline loop — no MLOps infra, no auto-retraining. (A fourth candidate, outcome tracking, was cut during clarification — see Out of Scope.)"

## Overview

The pipeline already produces, for every job it keeps, three comparable signals
(`score_gemini`, `score_vetor_desc`, `score_title`) and the web UI already captures the
user's judgments (`status`, `error_class`). Those judgments are an unused labeled dataset.
This feature closes the loop: it turns the user's own triage into (a) a measurement of how
well each signal predicts the user's decisions, (b) a fused calibrated score, and (c) a
feedback signal that steers the LLM toward the user's real taste. The loop stays **manual
and offline** — it produces reports and artifacts a human inspects, not an automated
retraining system. This is a single-user personal tool with a small label volume; the
scope is deliberately the three pieces with the best value-per-effort and nothing more. (A fourth
candidate — structured outcome tracking — was considered and cut; see Out of Scope.)

## Clarifications

### Session 2026-07-22

- Q: What counts as a positive vs. negative label from the existing fields? → A:
  **Positive** = `status` == `applied`; **Negative** = `error_class` == `Escopo incorreto`.
  (Correction after inspecting the real history: the persisted `status` vocabulary is
  **English** — `new`/`viewed`/`applied`/`error`, per `webapp.py VALID_STATUSES` — even
  though the UI labels are Portuguese ("Novo/Visto/Inscrito"). Earlier drafts wrote
  `inscrito`/`visto`, which never match stored data.) **`viewed` is excluded/neutral** —
  "looked at", not "would apply". **`irrelevant` is EXCLUDED, not negative** — it is
  mostly AUTO-assigned by the pipeline for sub-bar jobs (`passes_relevance_filter`), so
  using it as ground truth would be circular (a threshold on the very scores we evaluate).
  Jobs with `status` in {`new`, `viewed`, `irrelevant`, `duplicado`} or no triage are
  **unlabeled**. `error_class` values `Localidade/Modelo incorreto` and `Não é CLT` are
  **excluded from match-quality evaluation** — location/contract errors, not role fit.
- Q: How is the frozen golden set (regression gate) selected and stored? → A: The **N
  most-recent labeled jobs** are snapshotted (with their labels) to a **separate frozen
  file** the first time the harness needs one; **N is env-configurable**. Evaluation always
  reads the frozen labels, never the live history, so re-triaging a job cannot move the gate.
- Q: Should the outcome-tracking piece (former US4) be built in this feature? → A: **No —
  cut it.** Its value is speculative (future, volume-dependent) and depends on manual
  discipline to record results; it delivers nothing now. Deferred to Out of Scope; if wanted
  later, jot outcomes in the existing free-text `notes` before adding a structured field.
  The feature now covers **three** pieces (harness, few-shot, meta-model).
- Q: Does this feature change the live pipeline's scoring or filtering? → A: **Only piece 3
  (few-shot) touches the live scoring path**, and only by enriching the LLM prompt — it does
  NOT change any drop/keep decision, threshold, or the default-keep policies. The harness and
  meta-model are **offline** (run by hand, read-only over history). Wiring the meta-model
  into the pipeline is explicitly out of scope for this feature.
- Q: What happens when there are too few labels? → A: Every piece **degrades gracefully and
  says so**. Below a configurable minimum label count, the harness reports "insufficient
  labels" per metric instead of a misleading number, the meta-model is skipped, and few-shot
  falls back to today's exemplar-free prompt. No piece may fabricate confidence from thin data.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Measure whether each score predicts the user's decisions (Priority: P1)

The user runs a single offline command that reads the job history, treats their own triage
marks as ground truth, and reports how well each of the three existing scores separates jobs
they engaged with from jobs they rejected — as label-conditioned distributions, a separation
metric (AUC), and a suggested threshold per score. The report is validated **temporally**
(measured on recent labels using thresholds derived from older ones) and **sliced** (per
search term, and Portuguese vs. English postings) so a signal that works overall but inverts
in one language or role is visible rather than hidden in the average.

**Why this priority**: This is the engine of the whole loop. Without an honest measurement,
every other change (threshold tweak, model swap, prompt edit) is a guess. It is a complete,
demonstrable slice on its own — it delivers value the day it runs, before any other piece
exists.

**Independent Test**: Run the harness against the current history and confirm it prints and
writes a report showing per-score separation of positives vs. negatives, a temporal split, and
per-slice breakdowns — with an explicit "insufficient labels" note for any slice below the
minimum.

**Acceptance Scenarios**:

1. **Given** a history with labeled jobs, **When** the user runs the harness, **Then** it
   reports, for each of `score_gemini`, `score_vetor_desc`, `score_title`: the score
   distribution among positives vs. negatives, a separation metric, and a suggested threshold.
2. **Given** labeled jobs spanning a time range, **When** the harness runs, **Then** it derives
   thresholds/metrics on the older portion and reports how they hold on the most recent
   portion (temporal validation), never a random shuffle.
3. **Given** jobs from multiple search terms and both languages, **When** the harness runs,
   **Then** it reports separation per search term and PT vs. EN, flagging any slice where a
   score inverts (negatives scoring higher than positives).
4. **Given** a slice (or the whole set) with fewer than the configured minimum labels, **When**
   the harness runs, **Then** it emits an explicit "insufficient labels (n=X, need Y)" note for
   that slice instead of a number.
5. **Given** a completed run, **When** it finishes, **Then** the report is both printed and
   written to a file, with no dashboard or server involved.

---

### User Story 2 - Steer the LLM with the user's own accept/reject examples (Priority: P2)

The match prompt sent to the LLM includes a small, bounded set of concrete examples drawn from
the user's own history — jobs they accepted (`inscrito`) and jobs they rejected as out of scope
— so the LLM's `match_score` aligns with the user's demonstrated taste and improves as they keep
triaging, with no retraining. When too few labels exist, the prompt falls back to today's
exemplar-free form.

**Why this priority**: The LLM score is what actually orders the jobs the user sees. Injecting
the user's real decisions is the single highest-leverage, lowest-cost way to improve that score,
and it is a self-reinforcing loop: more triage → better exemplars → better alignment.

**Independent Test**: With enough labeled history, trigger a match and confirm the prompt sent to
the LLM contains accept/reject exemplars drawn from history, is bounded in size, and that with
history below the minimum the prompt is byte-identical to today's exemplar-free prompt.

**Acceptance Scenarios**:

1. **Given** sufficient labeled history, **When** a job is matched, **Then** the LLM prompt
   includes a bounded number of accept and reject exemplars drawn from the user's history.
2. **Given** the exemplar set, **When** the prompt is built, **Then** its total size respects a
   configurable token/character budget — exemplars are capped and truncated, never unbounded.
3. **Given** history below the configured minimum label count, **When** a job is matched, **Then**
   the prompt is the current exemplar-free prompt (graceful fallback), and the run behaves exactly
   as today.
4. **Given** the exemplars are injected, **When** the LLM responds, **Then** the response contract
   (`match_score`, `core_role_compatible`, `strengths`, `gaps`, `verdict`) is unchanged and no
   drop/keep decision or threshold changes — only the score's alignment improves.
5. **Given** the candidate's own profile/company data appears in exemplars, **When** the prompt is
   built, **Then** only data already present in the local history is used (no new data source, no
   new external exposure beyond the existing LLM call).

---

### User Story 3 - Fuse the three scores into one calibrated probability (Priority: P3)

The user runs an offline step that trains a simple, interpretable model on their labels to combine
the three scores into a single calibrated "probability I will apply". The model's coefficients make
explicit how much each score contributes — in particular, whether the paid/quota-limited
`score_gemini` adds predictive value over the two free cosines. The trained model is saved as an
artifact and its quality is reported through the harness. Wiring it into the live pipeline is not
part of this feature.

**Why this priority**: It answers the standing question — "is the LLM worth its cost?" — with
coefficients instead of intuition, and yields a better single ranking signal. It is lower priority
than the harness (which it depends on for evaluation) and the few-shot (which improves the score
that actually drives the product today).

**Independent Test**: With enough labeled history, run the training step and confirm it produces a
saved model artifact plus a report of its calibrated accuracy and per-score coefficients; with
history below the minimum it skips cleanly and says why.

**Acceptance Scenarios**:

1. **Given** sufficient labeled history, **When** the user runs the training step, **Then** it
   produces a saved model artifact and reports each score's contribution (coefficient) to the
   combined prediction.
2. **Given** the trained model, **When** the harness evaluates it, **Then** it reports the model's
   separation and calibration on a temporally held-out portion, comparable to the individual scores.
3. **Given** the coefficients, **When** the report is read, **Then** it is possible to state whether
   `score_gemini` earns its cost relative to `score_vetor_desc` and `score_title`.
4. **Given** history below the configured minimum, **When** the training step runs, **Then** it
   skips model training and reports "insufficient labels" rather than fitting an unreliable model.
5. **Given** the model exists as an artifact, **When** the pipeline runs, **Then** the live scoring
   and filtering behavior is unchanged (the model is not yet wired in).

---

### Edge Cases

- **No labels at all**: the harness runs without error and reports "insufficient labels" for every
  metric; the meta-model is skipped; few-shot falls back to the exemplar-free prompt. Nothing crashes.
- **All labels one class** (only positives, or only negatives): separation metrics that require both
  classes are reported as not-computable with a reason, not as a misleading value.
- **Label meaning drift**: because triage happens over months, thresholds are always reported on a
  recent temporal slice so a criterion that changed over time is visible.
- **A job re-labeled by the user** (e.g. `visto` → `Escopo incorreto`): the harness uses the current
  label at read time; there is no separate label store to fall out of sync.
- **Exemplar leakage**: a job used as a few-shot exemplar is drawn only from history; it must not
  cause the same job to be re-scored differently in a way that changes a drop/keep decision.
- **Golden set drift**: if the frozen golden set's jobs are later re-triaged, the regression gate must
  use the labels captured when the set was frozen, not the live ones, or the gate moves under itself.
## Requirements *(mandatory)*

### Functional Requirements

**Evaluation harness (US1)**

- **FR-001**: The system MUST provide an offline command that reads the shared job history and, using
  the user's triage marks as ground truth, reports per-score (`score_gemini`, `score_vetor_desc`,
  `score_title`) separation of positive from negative labels, including label-conditioned
  distributions, a separation metric (AUC or equivalent), and a suggested threshold per score.
- **FR-002**: The harness MUST define positives as `status` == `applied` and negatives as
  `error_class` == `Escopo incorreto` (the persisted status vocabulary is English —
  `new`/`viewed`/`applied`/`error`). It MUST treat `viewed` as excluded/neutral, MUST exclude
  `irrelevant` from both classes (it is auto-assigned by the pipeline, so counting it would be
  circular), and MUST exclude `error_class` values `Localidade/Modelo incorreto` and `Não é CLT`
  and untriaged/`new`/`duplicado` jobs from match-quality evaluation.
- **FR-003**: The harness MUST validate temporally — derive thresholds/metrics on an older portion and
  report how they hold on the most recent portion — and MUST NOT use a random train/test shuffle.
- **FR-004**: The harness MUST report metrics per slice — per search term (**approximated by the
  nearest search term**, since the matching term is not stored in history; see plan research D5) and
  Portuguese vs. English postings — and flag any slice where a score inverts relative to labels.
- **FR-005**: The harness MUST maintain a frozen golden set formed from the **N most-recent labeled
  jobs** (N env-configurable), snapshotted with their labels to a **separate file** on first creation,
  and evaluate against it as a regression gate. Evaluation MUST read the frozen labels (not the live
  history) so re-triaging a job cannot move the gate, and any model/prompt/threshold change can be
  checked for regression.
- **FR-006**: The harness MUST print the report and also write it to a file; it MUST NOT require a
  dashboard, server, or browser.
- **FR-007**: The harness MUST read the history read-only and MUST NOT modify `vagas_historico.json`.

**Few-shot from the user's decisions (US2)**

- **FR-008**: The system MUST include, in the LLM match prompt, a bounded set of accept exemplars
  (from `inscrito` jobs) and reject exemplars (from `Escopo incorreto`/`irrelevant` jobs) drawn from
  the history, so the LLM `match_score` aligns with the user's demonstrated decisions.
- **FR-009**: The exemplar selection MUST be bounded by a configurable count and a token/character
  budget; exemplars MUST be capped and truncated so the prompt never grows unbounded.
- **FR-010**: When labeled history is below a configurable minimum, the system MUST fall back to the
  current exemplar-free prompt with identical behavior.
- **FR-011**: Injecting exemplars MUST NOT change the LLM response contract nor any drop/keep decision,
  threshold, or the default-keep policies (Principles I & II) — it only enriches the prompt.

**Meta-model (US3)**

- **FR-012**: The system MUST provide an offline step that trains a simple, interpretable model on the
  user's labels to fuse the three scores into a single calibrated probability of applying, and saves it
  as an artifact.
- **FR-013**: The training step MUST report each score's contribution (coefficient) so it is possible to
  judge whether `score_gemini` earns its cost relative to the two free cosines.
- **FR-014**: The meta-model MUST be evaluated on the **same temporal held-out split the harness uses**
  (separation + calibration), reported alongside the individual scores' metrics so the two are
  comparable. (The metrics are computed in the meta-model step, not routed through `eval_scores.py`;
  the split definition is shared.)
- **FR-015**: When labeled history is below the configured minimum, the training step MUST skip and
  report "insufficient labels" rather than fit an unreliable model.
- **FR-016**: The meta-model MUST NOT be wired into the live pipeline scoring/filtering in this feature;
  the pipeline's runtime behavior MUST be unchanged by its existence.

**Cross-cutting**

- **FR-017**: All new tunables (minimum label counts, exemplar count/budget, golden-set size, temporal
  split point, thresholds) MUST be environment-configurable via `src/settings.py` helpers with sensible
  defaults (Principle IV).
- **FR-018**: No piece of this feature may cause a good job to be silently dropped; the default-CLT and
  default-in-scope policies (Principles I & II) MUST be preserved.
- **FR-019**: Every piece MUST degrade gracefully on insufficient/edge-case data (no labels, single
  class, tiny slices) and report the limitation explicitly rather than emitting a misleading result.

### Key Entities *(include if feature involves data)*

- **Label**: a per-job ground-truth signal derived at read time from existing user fields — positive
  (`status == applied`), negative (`error_class == Escopo incorreto`), or excluded/unlabeled (`viewed`,
  `new`, `irrelevant`, `duplicado`, untriaged, or the location/contract error classes). Not a new
  stored field; computed from `status`/`error_class`.
- **Score triple**: the three existing per-job signals (`score_gemini`, `score_vetor_desc`,
  `score_title`) already persisted in history — the features under evaluation.
- **Evaluation report**: the printed + on-disk output — per-score separation, thresholds, temporal
  holdout results, per-slice breakdown, golden-set regression status.
- **Golden set**: the N most-recent labeled jobs, snapshotted with their labels to a separate frozen
  file on first creation, used as a regression gate across changes (labels read from the snapshot, not
  live history).
- **Meta-model artifact**: the saved trained model plus its coefficients and calibration metrics.
- **Exemplar set**: the bounded accept/reject examples selected from history for the LLM prompt.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: From a single command and the current history, the user obtains a written report stating,
  for each of the three scores, how well it separates their accepted from rejected jobs and a suggested
  threshold — with zero manual data preparation.
- **SC-002**: The report distinguishes recent performance from historical (temporal holdout) and breaks
  results down by search term and by language, so a score that inverts in any single slice is visible in
  the report rather than hidden in the average.
- **SC-003**: After enough triage, the user can read from the meta-model's coefficients whether the LLM
  score adds predictive value over the two free cosines — a direct, evidence-based answer to "is the LLM
  worth keeping".
- **SC-004**: The user can compare two harness reports — few-shot disabled vs enabled, across the
  enable date — and observe whether agreement between the LLM `match_score` and their subsequent
  accept/reject decisions improved on the recent temporal slice. (Operational before/after comparison;
  the harness does not auto-attribute the delta to few-shot.)
- **SC-005**: Any change (threshold, model, prompt) can be checked against the frozen golden set and the
  user can tell whether it regressed, before adopting it.
- **SC-006**: With no or minimal labels, every piece runs without error and reports its limitation
  explicitly; no misleading metric, no crash, and the live pipeline behaves exactly as before this
  feature.
- **SC-007**: User triage marks (`status`, `notes`, `error_class`) made while the harness or few-shot
  reads history are never clobbered — the read-only offline tools and the prompt enrichment leave the
  merge-don't-overwrite behavior of the shared history unchanged.

## Assumptions

- **Labels are the existing fields, read live**: ground truth is derived from `status` and `error_class`
  at read time; no separate labeling UI or label store is introduced. Re-triaging a job simply changes
  its label next run.
- **Positive/negative definition** (resolved — see Clarifications; corrected to real data): positive =
  `status == applied`; negative = `error_class == Escopo incorreto`; `viewed` is excluded/neutral
  (looked-at ≠ would-apply); `irrelevant` is excluded (auto-assigned, would be circular);
  `Localidade/Modelo incorreto` and `Não é CLT` are excluded as non-role-fit errors.
- **Small, single-user label volume**: the loop is manual and offline — a human runs the harness/training
  and decides. No scheduling, no automation of retraining, no experiment-tracking service.
- **Offline-first, minimal live-path change**: only few-shot (US2) touches the running pipeline, and only
  by enriching the prompt; the harness and meta-model are read-only offline tools. Adopting the
  meta-model in the pipeline is a separate, later decision.
- **Reuses existing artifacts**: the three scores, the history file, and the LLM prompt builder already
  exist; this feature reads and lightly extends them rather than introducing a new data pipeline.
- **Graceful degradation is mandatory, not optional**: thin data must yield an honest "insufficient" note,
  never a fabricated number — this is a first-class requirement (FR-019), consistent with the project's
  never-mislead posture.

## Explicitly Out of Scope (deferred, do NOT build)

The following are intentionally excluded; they are not justified at this tool's scale and would be
premature. Documented here so "deferred" does not silently become "forgotten":

- MLflow or any experiment-tracking service/infrastructure.
- Drift-alerting infrastructure (monitoring dashboards, alerts).
- An active-learning UI that surfaces the most informative jobs to label.
- DSPy-style automated prompt optimization.
- Shadow-deployment orchestration for comparing models on live traffic.
- Any automatic retraining or automatic promotion of a new model/threshold/prompt.
- Wiring the meta-model into the live pipeline's scoring/filtering (US3 produces the artifact only).
- **Outcome tracking** (a structured application-result field in the web UI) — entirely deferred. Its
  value is speculative and volume-dependent, and it depends on manual discipline to record results.
  If wanted later, the lazy path is to jot the outcome in the existing free-text `notes`, and add a
  structured field only once outcomes are actually being recorded at useful volume.

## Dependencies

- Reads the shared job history (`vagas_historico.json`) — the single source of truth — for scores and
  labels.
- Depends on the existing LLM match prompt builder for the few-shot injection point.
- Reuses the `src/settings.py` env-config convention for all new tunables.

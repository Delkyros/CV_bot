# Feature Specification: Pipeline Run Control & Scheduling

**Feature Branch**: `001-pipeline-run-control`

**Created**: 2026-07-05

**Status**: Draft

**Input**: User description: "Controle do pipeline via API no app Flask, com progresso em tempo real e agendamento automático — disparar o fluxo pela UI, ver quanto falta enquanto roda, impedir disparo concorrente, e mostrar countdown da próxima ativação automática com botão de reiniciar."

## Clarifications

### Session 2026-07-05

- Q: What is the run controller's state machine, and how does the single trigger
  control behave in each state? → A: **Three states.**
  - **Waiting (idle, counting down)**: the trigger control is **enabled** and displays
    the **countdown to the next automatic run on the control itself**.
  - **Running**: the trigger control is **disabled** and a **progress bar** is shown.
  - **Trigger fires** — when the countdown elapses (interval passed) **AND** no run is in
    progress, **or** the user clicks the enabled control — the system **runs the
    pipeline** and the state becomes **Running**.
- Q: Who owns scheduling — the in-app controller or the existing container interval
  loop? → A: The **dashboard app owns scheduling**. It holds the countdown and the
  "interval elapsed AND idle → run" trigger. The existing `docker-entrypoint.sh`
  interval loop MUST be disabled in the deployment where the dashboard owns scheduling,
  so the pipeline is never started twice.
- Q: Does an automatic trigger fire while a run is already in progress? → A: **No.** The
  automatic trigger fires only when not currently running ("passou 6h e não estou
  rodando agora"); if a run is in progress when the interval elapses, no second run
  starts and the next cycle is scheduled instead.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Trigger a pipeline run from the dashboard (Priority: P1)

The user opens the web dashboard and starts a full pipeline run (scrape → classify →
dedupe → match → report) with a single action, instead of relying on a CLI or the
container scheduler. The dashboard immediately confirms that a run has started, and if
a run is already in progress it refuses to start a second one and says so.

**Why this priority**: This is the core of the feature — putting control of the
pipeline in the user's hands from the UI. Without it, nothing else matters. It is a
complete, demonstrable slice on its own.

**Independent Test**: From the dashboard, click "Run now" with no run in progress and
confirm the run starts; click it again while running and confirm the second attempt is
rejected with a clear "already running" state.

**Acceptance Scenarios**:

1. **Given** no run is in progress, **When** the user triggers a run, **Then** the
   system starts the pipeline in the background and the dashboard shows a "running"
   state without the browser waiting for the pipeline to finish.
2. **Given** a run is already in progress, **When** the user (or a second browser tab)
   triggers a run, **Then** the system rejects it, keeps the existing run untouched,
   and the dashboard communicates "already running".
3. **Given** a run is in progress, **When** the user views the dashboard, **Then** the
   trigger control is visibly disabled.
4. **Given** a run finishes (successfully or with an internal failure), **When** the
   user views the dashboard, **Then** the trigger control is enabled again and the
   outcome of the last run is visible.

---

### User Story 2 - See how much of the run is left, in real time (Priority: P2)

While a run is in progress, the dashboard shows live progress — the current stage and
how much is left — so the user knows the pipeline is alive and roughly when it will
finish, without watching logs.

**Why this priority**: Turns an opaque background job into an observable one. Valuable,
but only meaningful once triggering (US1) exists.

**Independent Test**: Trigger a run and, while it executes, observe the dashboard
progress indicator advance through the pipeline stages and reach completion when the
run ends.

**Acceptance Scenarios**:

1. **Given** a run is in progress, **When** the user views the dashboard, **Then** they
   see the current stage (e.g. scraping, matching, reporting) and a progress indication
   of how much remains, refreshed periodically while it runs.
2. **Given** the matching stage is processing jobs, **When** the user views progress,
   **Then** the indication reflects advancement through that stage (e.g. jobs processed
   out of jobs collected).
3. **Given** a run has just finished, **When** the user views the dashboard, **Then**
   progress reads as complete (100% / done) and stops advancing.
4. **Given** no run is in progress, **When** the user views the dashboard, **Then** the
   progress area shows an idle/last-run state rather than a stale in-progress bar.

---

### User Story 3 - Automatic recurring runs with a visible countdown (Priority: P3)

The pipeline runs automatically on a recurring cycle. The dashboard shows a countdown
to the next automatic run. The user can start a run manually before the countdown
elapses; doing so resets the cycle so the next automatic run is measured from the
manual trigger.

**Why this priority**: Automates freshness and gives the user predictability, but the
system is already useful with manual runs (US1) and observability (US2) alone.

**Independent Test**: With a short configured interval, observe the countdown reach
zero and an automatic run start; separately, trigger a manual run mid-countdown and
confirm the countdown resets to a full interval from that moment.

**Acceptance Scenarios**:

1. **Given** the system is idle, **When** the user views the dashboard, **Then** they
   see a countdown to the next automatic run.
2. **Given** the countdown reaches zero and no run is in progress, **When** the moment
   arrives, **Then** the system starts a run automatically.
3. **Given** the countdown reaches zero while a run is already in progress, **When** the
   moment arrives, **Then** the system does NOT start a second concurrent run and
   schedules the next cycle instead.
4. **Given** a run (manual or automatic) starts, **When** it starts, **Then** the
   countdown to the next automatic run is reset to a full interval measured from that
   start.

---

### Edge Cases

- **App restart mid-run**: if the process hosting the pipeline stops while a run is in
  progress, on restart the system MUST report an idle/last-known state and allow a new
  run — it must not stay stuck "running" forever.
- **Run fails internally**: a pipeline that errors out MUST release the run lock, mark
  the last run as failed with a reason, and let the schedule/manual trigger continue
  (never leave the trigger permanently disabled).
- **Two triggers within the same instant**: only one run may acquire the lock; the
  other is rejected.
- **Very fast or empty run** (e.g. nothing new to scrape): progress still reaches a
  completed state and the trigger re-enables.
- **Interval misconfiguration** (missing/invalid value): the system falls back to the
  documented default interval rather than failing to schedule.
- **Countdown while paused/disabled** (if scheduling is turned off via configuration):
  the dashboard indicates automatic runs are off rather than showing a stale countdown.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST let the user start a full pipeline run on demand from the
  dashboard, returning control to the user immediately (the run executes in the
  background; the request does not block until completion).
- **FR-002**: The system MUST enforce single-run concurrency: at most one pipeline run
  executes at a time. A trigger attempt while a run is in progress MUST be rejected with
  a clear "already running" outcome and MUST NOT start, queue, or disturb the running one.
- **FR-003**: The dashboard MUST use a single trigger control with two enabled-state
  presentations: while **idle/waiting** it is **enabled and displays the countdown to
  the next automatic run on the control itself**; while a run is **in progress** it is
  **disabled and accompanied by a progress bar**. It re-enables (and resumes showing the
  countdown) once the run ends (success or failure).
- **FR-004**: The system MUST expose the current run state (idle / running / last-run
  outcome) to the dashboard, refreshable while a run is in progress.
- **FR-005**: While a run is in progress, the system MUST report progress including the
  current pipeline stage and a measure of how much of the run remains, updated as the
  run advances.
- **FR-006**: The system MUST run the pipeline automatically on a recurring interval
  without user action.
- **FR-007**: The recurring interval (and whether automatic scheduling is enabled) MUST
  be configurable via environment variables with a sensible default, per project
  Principle IV.
- **FR-008**: The dashboard MUST show a countdown to the next automatic run when
  scheduling is enabled, presented on the trigger control while idle (per FR-003).
- **FR-009**: Starting any run (manual or automatic) MUST reset the countdown so the
  next automatic run is measured from that start time.
- **FR-010**: An automatic trigger that would collide with an in-progress run MUST be
  skipped (no concurrent run), and the next cycle scheduled.
- **FR-011**: A run that fails MUST release the concurrency lock, record that the last
  run failed and why, and leave the system able to start subsequent runs — the pipeline
  never crashes the controlling app (Principle VI).
- **FR-012**: The pipeline's job data MUST continue to be persisted to the single
  shared history with merge-don't-overwrite semantics; triggering runs from the
  dashboard MUST NOT change how user triage marks are preserved (Principle III).
- **FR-013**: On startup, the system MUST initialize to a consistent state (idle unless
  a run is genuinely active) so a crashed/interrupted prior run never leaves the trigger
  permanently blocked.
- **FR-014**: The dashboard app MUST be the sole owner of scheduling (the countdown and
  the "interval elapsed AND idle → run" trigger). In the deployment where the dashboard
  owns scheduling, the container interval loop (`docker-entrypoint.sh`) MUST be disabled
  so the pipeline is never started by two schedulers at once.

### Key Entities *(include if feature involves data)*

- **Run State**: the current status of the pipeline controller — one of idle, running,
  or finished; includes the active stage and progress while running, and the outcome
  (success/failure + reason) and timestamp of the most recent run.
- **Schedule**: the recurring-run configuration and derived next-run time — interval,
  whether enabled, and the timestamp the next automatic run is due (from which the
  countdown is computed).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user can start a pipeline run from the dashboard in a single action and
  see it acknowledged as "running" within 2 seconds, without the page hanging.
- **SC-002**: At no time do two pipeline runs execute simultaneously; every concurrent
  trigger attempt during a run is rejected with a clear message (0 duplicate runs).
- **SC-003**: While a run is in progress, the displayed progress advances at least once
  per pipeline stage and reaches a completed state within a few seconds of the run
  actually finishing.
- **SC-004**: When idle and scheduling is enabled, the dashboard always shows a countdown
  whose value matches the time remaining until the next automatic run (within a small
  refresh margin).
- **SC-005**: Triggering a manual run resets the next automatic run to a full interval
  from the trigger, verifiable by observing the countdown jump back to (approximately)
  the full interval.
- **SC-006**: A run that fails internally never leaves the trigger permanently disabled —
  the system returns to an idle, trigger-enabled state and reports the failure.
- **SC-007**: User triage marks (status, notes, etc.) made while a run executes are
  preserved after the run completes (no clobbering), matching current behavior.

## Assumptions

- **Progress model**: "how much is left" is expressed as pipeline-stage progress
  (scrape → classify → dedupe → match → report) plus, within the match stage, a
  processed-vs-collected count — total work is not fully known until scraping finishes,
  so progress is coarse early and finer during matching. A single percentage plus a
  stage label is sufficient for the dashboard.
- **Default interval**: the recurring interval reuses the project's existing
  `RUN_INTERVAL_SECONDS` convention (default 6 hours), configurable via env; a value of
  0 / disabled means "no automatic scheduling" (manual only), consistent with the
  current run-once behavior.
- **Single-user, trusted dashboard**: no authentication or multi-user coordination is in
  scope; the dashboard is assumed to be operated by its single owner.
- **In-memory run state is acceptable**: run/progress state lives with the controlling
  app instance; a persistent audit log of past runs is out of scope beyond what the
  schedule needs. App restart resets to idle.
- **The pipeline logic is unchanged**: this feature wraps and observes the existing
  `main.py` flow; it does not alter scraping, contract classification, matching, or
  filtering rules.
- **Progress polling from the front is acceptable** for real-time updates (the front
  queries run state periodically while a run is active); no hard requirement for
  server-push.
- **Reconciliation with the container scheduler** *(resolved — see Clarifications and
  FR-014)*: the dashboard app owns scheduling; the existing `docker-entrypoint.sh`
  interval loop MUST be disabled in that deployment so the pipeline is never started by
  two schedulers. How that disabling is wired (compose profile, env flag, separate
  service) is a planning concern.

## Dependencies

- Reuses the existing pipeline entrypoint (`main.py` flow) and the shared job history as
  the single source of truth.
- Depends on the existing `RUN_INTERVAL_SECONDS` environment convention for the default
  interval.

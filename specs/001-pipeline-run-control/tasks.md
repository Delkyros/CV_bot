---
description: "Task list for Pipeline Run Control & Scheduling"
---

# Tasks: Pipeline Run Control & Scheduling

**Input**: Design documents from `specs/001-pipeline-run-control/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Included for the controller's non-trivial logic (concurrency lock, state
transitions, schedule math, boot recovery) — matches the project's pytest convention and
the constitution's "one runnable check behind non-trivial logic". UI is validated via
quickstart, not automated tests.

**Organization**: Grouped by user story. Each story is an independently deliverable
increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on incomplete tasks)
- **[Story]**: US1 / US2 / US3 (setup, foundational, and polish have no story label)

## Path Conventions

Single project: `webapp.py`, `main.py`, `src/`, `tests/`, `web/` at repo root.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Register the new configuration surface.

- [X] T001 Document new env tunables in `.env.example` and the README Tunables table:
  `RUN_INTERVAL_SECONDS` (reused, default 21600), `SCHEDULER_ENABLED` (default true),
  `RUN_STATE_PATH` (default `run_state.json`), `RUN_START_ON_BOOT` (default false).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The run controller and its state file — shared by all three stories.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T002 Create `src/run_controller.py` with atomic RunState read/write helpers
  (temp file + `os.replace`, path from `RUN_STATE_PATH`) implementing the
  single-writer-per-key merge from `contracts/run-state.md`.
- [X] T003 Implement `RunController` core in `src/run_controller.py`: in-memory run state
  + threading lock, `status` projection (per `contracts/http-api.md`), and boot
  reconciliation that resets a stale `running` (no live pid) to `idle` with
  `last_outcome="interrupted"` (FR-013).
- [X] T004 Implement subprocess spawn + monitor in `src/run_controller.py`: launch
  `[sys.executable, "main.py"]`, record `pid`/`run_started_at`, a monitor thread that on
  exit sets `last_outcome`/`exit_code`/`run_finished_at`, clears `pid`, and releases the
  lock even on failure (FR-011, Principle VI).
- [X] T005 [P] Tests in `tests/test_run_controller.py`: lock rejects a 2nd start while
  running; transitions idle→running→finished; boot reconciliation of a stale `running`.

**Checkpoint**: Controller can start/track/reconcile a run in isolation.

---

## Phase 3: User Story 1 - Trigger a run from the dashboard (Priority: P1) 🎯 MVP

**Goal**: A dashboard control starts a pipeline run in the background; a second trigger
while running is rejected; the control disables during a run.

**Independent Test**: Click "Run now" with nothing running → run starts; click again while
running → rejected with "already running"; control disabled during the run.

- [X] T006 [US1] Add `POST /api/run` in `webapp.py`: acquire the controller, start a run
  (manual), return `202` with run state, or `409 already_running` if a run is active
  (FR-001, FR-002); non-blocking (SC-001).
- [X] T007 [US1] Add minimal `GET /api/run/status` in `webapp.py` returning `status`,
  `last_outcome`, `run_started_at` (enough to enable/disable the control) (FR-004).
- [X] T008 [US1] Instantiate the `RunController` singleton in `webapp.py` (module load /
  `main()`), running boot reconciliation once at startup (FR-013).
- [X] T009 [US1] Wire the trigger control in `web/index.html`: button that POSTs `/api/run`,
  disables while `status==running`, shows "já em execução" on `409`, polls
  `/api/run/status` (FR-003).
- [X] T010 [P] [US1] Test in `tests/test_run_controller.py`: triggering while running is
  rejected (409-equivalent at controller level); trigger enabled again after run ends.

**Checkpoint**: MVP — user can run the pipeline from the UI, protected against double runs.

---

## Phase 4: User Story 2 - See how much of the run is left (Priority: P2)

**Goal**: Live progress (stage + percent) while a run executes.

**Independent Test**: Trigger a run and watch the progress bar advance through stages and
reach 100% when it ends.

- [X] T011 [US2] Add best-effort progress emission to `main.py` at stage boundaries and
  every N matched jobs, writing only the `progress` key of `run_state.json` via the
  helper from T002; wrapped in try/except so a write failure never aborts the pipeline
  (FR-005, Principle VI). Weights per `research.md` D4.
- [X] T012 [US2] Extend `GET /api/run/status` in `webapp.py` with `progress`
  (stage/percent/detail/updated_at) and `server_time` (FR-004, FR-005).
- [X] T013 [US2] Add the progress bar to `web/index.html` (stage + percent from status);
  poll faster (~2s) while running, slower while idle.
- [X] T014 [P] [US2] Test in `tests/test_run_controller.py`: percent mapping helper
  (stage+counts → monotonic 0–100) and that reading status reflects the latest progress.

**Checkpoint**: Runs are observable end to end.

---

## Phase 5: User Story 3 - Automatic runs with a countdown (Priority: P3)

**Goal**: App-owned recurring schedule; countdown on the trigger control; manual trigger
resets the cycle.

**Independent Test**: With a short interval, countdown reaches zero → auto-run starts;
manual trigger mid-countdown resets it; countdown hitting zero during a run does not start
a second run.

- [X] T015 [US3] Implement the scheduler daemon thread in `src/run_controller.py`: tick
  loop that spawns an auto run when `now >= next_run_at` AND idle; skips (and reschedules)
  if a run is active (FR-010); sets `next_run_at = start + interval` on any run start
  (FR-009); honors `SCHEDULER_ENABLED` and `interval==0` = off (FR-006, FR-007).
- [X] T016 [US3] Start the scheduler thread from `webapp.py:main()`, respecting
  `RUN_START_ON_BOOT` for whether the first run fires immediately or after one interval.
- [X] T017 [US3] Extend `GET /api/run/status` with `next_run_at`, `scheduler_enabled`,
  `interval_seconds`; render the countdown on the trigger control in `web/index.html`
  (computed against `server_time`), showing "automatic runs off" when disabled (FR-008).
- [X] T018 [P] [US3] Test in `tests/test_run_controller.py`: `next_run_at` math on trigger
  (manual resets cycle); `interval==0` ⇒ scheduler off / `next_run_at` null; auto-trigger
  skipped when a run is in progress.

**Checkpoint**: All three stories functional and independently testable.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T019 Disable the container interval loop for the scheduler-owning deploy: set the
  pipeline service `RUN_INTERVAL_SECONDS=0` in `docker-compose.yml` and
  `docker-compose.example.yml`, with a comment that the web service now owns scheduling
  (FR-014).
- [X] T020 [P] Update README (web UI section) and add a note in `CLAUDE.md` about
  app-owned scheduling and the `run_state.json` channel (separate from history).
- [X] T021 Verify FR-012: assert/review that `RunController` and the endpoints never write
  `vagas_historico.json` (history writes stay in `main.save_job_history`); add a guard
  test if practical (SC-007).
- [X] T022 Run all `quickstart.md` scenarios (1–5) and the Docker single-scheduler check.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none — start immediately.
- **Foundational (Phase 2)**: after Setup — BLOCKS all user stories.
- **User Stories (Phase 3–5)**: all depend on Foundational. US1 is the MVP; US2 and US3
  build on the same controller but are independently testable increments.
- **Polish (Phase 6)**: after the desired stories are done.

### User Story Dependencies

- **US1 (P1)**: needs Foundational only. MVP.
- **US2 (P2)**: needs Foundational; extends the status endpoint US1 introduced but adds
  its own progress path — testable on its own by triggering a run.
- **US3 (P3)**: needs Foundational; extends status again and adds the scheduler thread —
  testable on its own with a short interval.

### Within Each Story

- Controller/model changes before endpoint changes before UI changes.
- Tests marked [P] touch `tests/test_run_controller.py` independently of the UI.

### Parallel Opportunities

- T005, T010, T014, T018 (tests) are [P] — but all live in `tests/test_run_controller.py`;
  run them in parallel only if split into distinct test functions/files to avoid edit
  conflicts.
- T020 (docs) is [P] with code polish tasks.
- With one developer, follow the phase order; the [P] markers mostly denote "no logic
  dependency", not separate files here.

---

## Parallel Example: User Story 1

```bash
# After Foundational (T002–T005):
Task: "T006 POST /api/run in webapp.py"
Task: "T009 trigger control in web/index.html"   # different file, parallelizable with T006
# T007 (status endpoint) and T008 (singleton) are in webapp.py — sequential with T006.
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 Setup → 2. Phase 2 Foundational → 3. Phase 3 US1.
4. **STOP and VALIDATE**: run the pipeline from the UI; confirm the double-trigger guard.
5. Deploy/demo — this alone replaces CLI triggering.

### Incremental Delivery

- US1 (trigger + guard) → US2 (progress) → US3 (schedule + countdown). Each ships value
  without breaking the previous. Finish with Phase 6 (Docker reconciliation + docs +
  quickstart).

---

## Notes

- [P] = no logic dependency; watch for same-file edits (`webapp.py`, `run_controller.py`,
  `test_run_controller.py` are touched by multiple tasks — sequence those).
- The most delicate task is T002/T011: two writers on `run_state.json` (controller owns
  lifecycle keys, pipeline owns `progress`). Keep single-writer-per-key + atomic replace.
- Commit after each task or logical group; stop at any checkpoint to validate.
- Constitution: T011 must not let a progress-write failure abort the pipeline (VI); T021
  guards history integrity (III); T001/T015 keep everything env-configurable (IV).

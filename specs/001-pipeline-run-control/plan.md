# Implementation Plan: Pipeline Run Control & Scheduling

**Branch**: `001-pipeline-run-control` | **Date**: 2026-07-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-pipeline-run-control/spec.md`

## Summary

Put pipeline control in the Flask dashboard: a single trigger control that runs the
pipeline on demand, a live progress indicator while it runs, single-run concurrency,
and an app-owned recurring schedule with a countdown to the next automatic run. Runs
are executed as a **subprocess** (`python main.py`) so the pipeline's `sys.exit()` calls
and failures stay isolated from the web server. The pipeline reports progress by writing
a small **`run_state.json`** file (atomic write, same pattern as the history); the Flask
app reads it and combines it with subprocess liveness to answer status polls. A
background daemon thread owns the schedule (interval elapsed AND idle → spawn). The
existing `docker-entrypoint.sh` interval loop is disabled in the dashboard-owned
deployment to avoid double runs.

## Technical Context

**Language/Version**: Python 3.12 (venv `.venv/Scripts/python.exe`, 3.12.10)

**Primary Dependencies**: Flask (already present, `flask>=3.0,<4`); Python stdlib
`subprocess`, `threading`, `json`, `tempfile`, `os`, `datetime`. **No new dependency**
(no APScheduler/Celery/websocket lib — a daemon thread + polling covers it).

**Storage**: JSON files. New `run_state.json` (run/progress/schedule state) alongside the
existing `vagas_historico.json`; both under `./data` in Docker. `run_state.json` is
SEPARATE from the history — the controller never writes job history (preserves
Principle III).

**Testing**: pytest (`.venv/Scripts/python.exe -m pytest -q`).

**Target Platform**: Local (Windows/Linux) + Docker Linux container serving the web UI.

**Project Type**: Single project — Flask web service (`webapp.py`) + batch pipeline
(`main.py`), shared `src/`.

**Performance Goals**: Trigger acknowledged < 2 s (SC-001); status poll cheap (read one
small JSON) at ~1–2 s cadence from the front; a run takes minutes.

**Constraints**: Never crash the web app on pipeline failure (Principle VI); merge-don't-
overwrite history untouched (Principle III); interval + enable flag env-configurable
(Principle IV); exactly one scheduler owns runs (FR-014).

**Scale/Scope**: Single user, single dashboard instance, at most one concurrent run.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Impact | Verdict |
|-----------|--------|---------|
| I. Default-CLT | Pipeline logic untouched; only wrapped/observed. | ✅ Pass |
| II. Default-In-Scope | Filter logic untouched. | ✅ Pass |
| III. Merge-Don't-Overwrite State | `run_state.json` is a separate file; history writes still flow through `save_job_history` only, inside the subprocess. Controller reads history but never writes it. | ✅ Pass |
| IV. Everything Env-Configurable | New tunables (`RUN_INTERVAL_SECONDS` reused, `SCHEDULER_ENABLED`, `RUN_STATE_PATH`, `RUN_START_ON_BOOT`) all env with defaults. | ✅ Pass |
| V. Portuguese Config, English Code | New env/code English; new UI labels Portuguese (match existing UI). | ✅ Pass |
| VI. Never-Crash Resilience | Subprocess isolation; controller catches spawn/monitor errors; a failed run releases the lock and records failure; web app keeps serving. | ✅ Pass |
| VII. Self-Adjusting Scope Filter | Untouched. | ✅ Pass |

**Result**: No violations. Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/001-pipeline-run-control/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions (subprocess, file IPC, scheduler)
├── data-model.md        # Phase 1 — run_state.json schema + entities
├── quickstart.md        # Phase 1 — how to validate end-to-end
├── contracts/
│   ├── http-api.md      # POST /api/run, GET /api/run/status contracts
│   └── run-state.md     # run_state.json file contract (pipeline↔web channel)
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
webapp.py                # + POST /api/run, GET /api/run/status; boot the controller
main.py                  # + light progress emission at stage boundaries + match loop
src/
├── run_controller.py    # NEW: run state, concurrency lock, subprocess spawn/monitor,
│                        #      scheduler thread, run_state.json read/write (atomic)
├── settings.py          # (reuse env helpers; no change expected)
└── ...                  # pipeline stages unchanged
web/
├── index.html           # + trigger control (countdown when idle / disabled + progress
│                        #   bar when running); poll GET /api/run/status
└── (existing assets)
tests/
├── test_run_controller.py   # NEW: lock, state transitions, crash recovery, schedule math
└── (existing tests unchanged)
docker-compose.yml       # disable entrypoint interval loop for the scheduler-owning
docker-compose.example.yml #  deployment (RUN_INTERVAL_SECONDS=0 on pipeline service;
                         #  web service owns scheduling)
```

**Structure Decision**: Single project. The new logic is isolated in
`src/run_controller.py`; `webapp.py` gains two endpoints and boots the controller;
`main.py` gains minimal progress emission (no logic change); the front polls a status
endpoint. Everything else is untouched.

## Complexity Tracking

> No constitution violations — section intentionally empty.

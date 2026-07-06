# Quickstart: Validate Pipeline Run Control & Scheduling

End-to-end checks that prove the feature works. Assumes the venv and a populated
`config/keywords.yaml` (see project README). Reference: [contracts](contracts/),
[data-model](data-model.md).

## Prerequisites

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Flask already present
# Optional: short interval so the schedule is observable during testing
export RUN_INTERVAL_SECONDS=120       # PowerShell: $env:RUN_INTERVAL_SECONDS=120
export SCHEDULER_ENABLED=true
```

Start the dashboard (now also the scheduler owner):

```bash
.venv/Scripts/python.exe webapp.py    # http://localhost:8000
```

## Scenario 1 — Manual trigger + concurrency guard (US1 / FR-001, FR-002, FR-003)

1. Open the dashboard; the trigger control is **enabled** and shows a **countdown**.
2. Click it (or `curl -X POST localhost:8000/api/run`) → expect `202` and status flips to
   `running`; the control becomes **disabled** with a **progress bar**.
3. While running, `curl -X POST localhost:8000/api/run` again → expect **`409`
   already_running**; the first run is untouched.

**Pass when**: exactly one run executes; the second trigger is rejected; the control is
disabled during the run and re-enabled after.

## Scenario 2 — Live progress (US2 / FR-004, FR-005, SC-003)

1. Trigger a run; poll `GET /api/run/status` (or watch the bar).
2. Observe `progress.stage` advance `scraping → dedupe → matching → reporting → done` and
   `progress.percent` increase (finest during `matching`, e.g. `matching 50/120`).
3. When the subprocess exits, status reaches `finished`/`idle`, `percent` = 100.

**Pass when**: progress advances at least once per stage and reaches 100% within a few
seconds of the run actually ending.

## Scenario 3 — Automatic run + countdown reset (US3 / FR-006..FR-010, SC-004, SC-005)

1. With `RUN_INTERVAL_SECONDS=120`, leave the dashboard idle and watch the countdown reach
   zero → an automatic run starts (`trigger: "auto"`).
2. Before the next countdown elapses, click the trigger → the countdown **resets to ~120s**
   from the click (`next_run_at = run_started_at + interval`).
3. Let the countdown hit zero **while a run is still in progress** (use a longer run) →
   no second run starts; the next cycle is scheduled.

**Pass when**: auto-run fires on zero; manual trigger resets the countdown; no concurrent
auto-run.

## Scenario 4 — Failure & crash recovery (FR-011, FR-013, SC-006)

1. Force a failing run (e.g. temporarily point `HISTORY_PATH` at an unwritable path, or
   break config) and trigger → the subprocess exits non-zero.
2. `GET /api/run/status` → `last_outcome: "failed"`, `last_error` set, control **enabled**
   again. The web app is still serving.
3. Simulate a crash mid-run: kill the pipeline subprocess, then restart `webapp.py` →
   boot reconciliation sets `last_outcome: "interrupted"`, `status: "idle"`, control
   enabled (not stuck "running").

**Pass when**: failures never disable the trigger permanently and never crash the web app.

## Scenario 5 — History integrity (FR-012, SC-007)

1. Trigger a run; while it executes, mark a job in the UI (status/notes).
2. After the run completes, reload → the mark is preserved (merge-don't-overwrite intact),
   and `run_state.json` never appears inside `vagas_historico.json`.

**Pass when**: user triage marks survive a concurrent run.

## Docker note (FR-014)

In the dashboard-owned deployment, the pipeline container's interval loop MUST be off so
runs aren't duplicated:

- Set the pipeline service `RUN_INTERVAL_SECONDS=0` (run-once / no loop), OR run only the
  web service and let its in-app scheduler own runs.
- Verify: over one interval, exactly one run executes (check `run_started_at` history in
  logs / `last_run` in the UI).

## Automated checks

```bash
.venv/Scripts/python.exe -m pytest tests/test_run_controller.py -q
```

Covers: concurrency lock rejects the 2nd trigger; state transitions idle→running→finished;
`next_run_at` math on trigger; boot reconciliation of a stale `running`; interval=0 ⇒
scheduler off.

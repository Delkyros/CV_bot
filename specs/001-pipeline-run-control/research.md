# Phase 0 Research: Pipeline Run Control & Scheduling

Decisions that resolve the "how" before design. All favor the laziest option that
satisfies the spec and the constitution (no new dependencies).

## D1 — Run the pipeline as a subprocess, not an in-process thread

- **Decision**: The controller runs the pipeline via `subprocess.Popen([sys.executable, "main.py"])`.
- **Rationale**: `main.py:main()` calls `sys.exit(0)` at several points (no new jobs, no
  in-scope jobs). In a thread that raises `SystemExit` inside the web server; as a
  subprocess it's a normal exit code. Subprocess also isolates crashes/memory and matches
  the user's "roda o sh" mental model. Liveness = `Popen.poll()`.
- **Alternatives rejected**:
  - *Thread calling `main.main()`* — `sys.exit` handling is fragile; a pipeline crash
    could take down the web server (violates Principle VI); shared interpreter state.
  - *Celery/RQ task queue* — heavy dependency + broker for a single-user, single-run app.

## D2 — Progress channel = a small `run_state.json` file (atomic write)

- **Decision**: The pipeline writes progress to `RUN_STATE_PATH` (default `run_state.json`,
  under `./data` in Docker) using the same temp-file-then-`os.replace` atomic pattern as
  `webapp._save_history`. The web app reads it on each status poll.
- **Rationale**: The pipeline is a separate process, so shared memory isn't available. A
  JSON status file is the project's existing idiom (files as the source of truth), needs
  no IPC library, and is trivially readable by the front via the web app. Atomic replace
  avoids torn reads.
- **Alternatives rejected**:
  - *stdout/log parsing* — brittle, couples progress to log format.
  - *Socket/pipe IPC* — more moving parts; no benefit at this scale.
  - *Shared DB / Redis* — new infrastructure for one small blob.

## D3 — Scheduler = one background daemon thread in the web app

- **Decision**: On web-app boot, start a daemon thread that loops: sleep a short tick
  (e.g. a few seconds), and when `now >= next_run_at` AND no run is in progress, spawn a
  run. `next_run_at` is recomputed as `run_start + RUN_INTERVAL_SECONDS` whenever a run
  starts (manual or automatic), satisfying FR-009.
- **Rationale**: stdlib `threading` covers a single periodic check; no APScheduler needed.
  The daemon thread dies with the process. Countdown is derived by the front from
  `next_run_at` (a timestamp returned by the status endpoint), so the server needn't push.
- **Alternatives rejected**:
  - *APScheduler* — new dependency for one interval timer.
  - *OS cron / keep `docker-entrypoint.sh`* — that's the double-run problem FR-014 forbids;
    the app must own scheduling so it can enforce the "idle" precondition and reset on
    manual trigger.
- **Consequence**: `docker-entrypoint.sh`'s interval loop MUST be disabled in this
  deployment (set the pipeline service to run-once / `RUN_INTERVAL_SECONDS=0`, and let the
  web service own scheduling). Documented in quickstart + compose.

## D4 — Progress model = weighted stages + match-loop counter

- **Decision**: Coarse stage weights, with fine-grained progress only during matching
  (the long stage): `scrape ≈ 40%`, `dedupe ≈ 5%`, `match ≈ 50%` (scaled by
  `jobs_done / jobs_total`), `report+save ≈ 5%`. The pipeline emits `{stage, percent,
  detail}` at each stage boundary and every N matched jobs.
- **Rationale**: Total work is unknown until scraping finishes (Assumption in spec), so a
  precise percentage isn't possible early; stage + a match counter is honest and enough
  for a progress bar. Weights are constants (tunable later if needed).
- **Alternatives rejected**:
  - *True ETA from timing history* — needs persisted run history (out of scope) and adds
    complexity for marginal value.
  - *Indeterminate spinner only* — fails FR-005 ("how much remains").

## D5 — Concurrency lock + crash recovery

- **Decision**: The controller holds the single source of truth for "is a run active":
  the live `Popen` handle (in memory) plus the `run_state.json` `status` + `pid`. A
  trigger is rejected if a run is active. On web-app boot, if `run_state.json` says
  `running` but no live subprocess owns it (fresh process, or `pid` not alive), the state
  is reset to `idle` with the last run marked interrupted (FR-013).
- **Rationale**: In-memory handle is authoritative while the app lives; the file lets a
  freshly booted app detect a stale "running" left by a crash. Single-instance assumption
  makes an in-process lock sufficient (no cross-process lock needed).
- **Alternatives rejected**:
  - *File lock only* — can't tell "running" from "crashed mid-run" without liveness.
  - *OS-level mutex* — overkill for one instance.

## D6 — Real-time updates = front polling

- **Decision**: The front polls `GET /api/run/status` on an interval (faster while
  running, slower/paused while idle) and renders the bar/countdown from the response.
- **Rationale**: The existing UI already fetches JSON; polling a tiny endpoint is simplest
  and robust. Matches spec Assumption ("polling acceptable, no server-push required").
- **Alternatives rejected**:
  - *Server-Sent Events / WebSocket* — more server plumbing for no required benefit at
    this cadence and scale.

## New environment tunables (Principle IV)

| Env var | Default | Meaning |
|---------|---------|---------|
| `RUN_INTERVAL_SECONDS` | `21600` (6h) | Recurring interval (reuses existing convention). `0` = scheduling off (manual only). |
| `SCHEDULER_ENABLED` | `true` | Master switch for the in-app scheduler thread. |
| `RUN_STATE_PATH` | `run_state.json` | Path to the run/progress state file (→ `./data` in Docker). |
| `RUN_START_ON_BOOT` | `false` | Whether the first automatic run fires immediately on boot or after one full interval. |

# Contract: HTTP API (web app)

Two new endpoints on the existing Flask app (`webapp.py`). JSON in/out. Single-user,
no auth (per spec scope).

## POST /api/run

Trigger a pipeline run on demand (manual trigger).

**Request**: no body required. Optional `{}`.

**Responses**:

- `202 Accepted` — run started.
  ```json
  {
    "ok": true,
    "status": "running",
    "trigger": "manual",
    "run_started_at": "2026-07-05T14:03:11",
    "next_run_at": "2026-07-05T20:03:11"
  }
  ```
- `409 Conflict` — a run is already in progress (FR-002); the existing run is untouched.
  ```json
  {
    "ok": false,
    "error": "already_running",
    "status": "running",
    "run_started_at": "2026-07-05T14:00:02",
    "progress": { "stage": "matching", "percent": 42, "detail": "matching 50/120" }
  }
  ```
- `500` — failed to spawn (rare); state remains `idle`, `last_error` set. The web app
  itself keeps serving (Principle VI).

**Acknowledgement time**: MUST return within ~2 s (SC-001); spawning is non-blocking.

## GET /api/run/status

Current controller state — polled by the front while running (fast) and idle (slow).

**Response** `200 OK` — the RunState projection (see data-model.md):

```json
{
  "status": "running",
  "trigger": "auto",
  "run_started_at": "2026-07-05T14:00:02",
  "run_finished_at": null,
  "progress": { "stage": "matching", "percent": 42, "detail": "matching 50/120", "updated_at": "2026-07-05T14:02:58" },
  "last_outcome": "success",
  "last_error": null,
  "exit_code": null,
  "scheduler_enabled": true,
  "interval_seconds": 21600,
  "next_run_at": "2026-07-05T20:00:02",
  "server_time": "2026-07-05T14:02:59"
}
```

Notes:
- `server_time` is included so the front computes the countdown against server clock
  (avoids client-clock skew): `seconds_remaining = next_run_at - server_time`.
- When `status == "idle"`, `progress` reflects the last run (or a zeroed idle state) and
  the trigger control is enabled with the countdown.
- When `scheduler_enabled == false`, `next_run_at` is null and the UI shows "automatic
  runs off" instead of a countdown (edge case in spec).

## UI contract (web/index.html)

- A single **trigger control**:
  - `status == idle` → **enabled**, shows countdown to `next_run_at` on the control.
  - `status == running` → **disabled**, a **progress bar** shows `progress.percent` and
    `progress.stage`/`detail`.
  - `status == finished`/back to idle → re-enabled, last outcome shown, countdown resumes.
- Poll `GET /api/run/status`: ~2 s while running, ~15–30 s (or on focus) while idle.
- Clicking the control `POST /api/run`; on `409` show "já em execução" and refresh status.

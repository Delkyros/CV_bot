# Phase 1 Data Model: Pipeline Run Control & Scheduling

The feature adds one small state blob. It does **not** touch the `vagas_historico.json`
schema (Principle III) — job history and run state are separate files.

## Entity: RunState (persisted to `run_state.json`)

The single record describing the controller's current state and the last run. Written by
the pipeline (progress) and by the web-app controller (lifecycle/schedule); read by the
web app to answer status polls. Atomic write (temp file + `os.replace`).

| Field | Type | Written by | Description |
|-------|------|-----------|-------------|
| `status` | enum: `idle` \| `running` \| `finished` | controller | Current controller state. `finished` collapses to `idle` for the UI once acknowledged; kept distinct so the last outcome is visible. |
| `pid` | int \| null | controller | OS pid of the active pipeline subprocess (null when idle). Used for crash recovery. |
| `run_started_at` | ISO-8601 str \| null | controller | When the current/last run started. |
| `run_finished_at` | ISO-8601 str \| null | controller | When the last run ended. |
| `trigger` | enum: `manual` \| `auto` \| null | controller | What started the current/last run. |
| `progress` | object (see below) | pipeline | Live progress; meaningful while `running`. |
| `last_outcome` | enum: `success` \| `failed` \| `interrupted` \| null | controller | Result of the last completed run. |
| `last_error` | str \| null | controller | Short reason when `last_outcome = failed`/`interrupted`. |
| `exit_code` | int \| null | controller | Subprocess exit code of the last run. |
| `next_run_at` | ISO-8601 str \| null | controller | When the next automatic run is due (null if scheduling off). Countdown source. |
| `scheduler_enabled` | bool | controller | Whether automatic scheduling is active. |
| `interval_seconds` | int | controller | Effective interval (echoes `RUN_INTERVAL_SECONDS`) for the UI to display. |

### Nested: `progress`

| Field | Type | Description |
|-------|------|-------------|
| `stage` | enum: `scraping` \| `dedupe` \| `matching` \| `reporting` \| `done` | Current pipeline stage. |
| `percent` | number 0–100 | Weighted overall progress (see research D4). |
| `detail` | str | Human-readable note, e.g. `"matching 37/120"`. |
| `updated_at` | ISO-8601 str | When progress was last written (freshness/staleness check). |

### State transitions

```text
        (boot: reconcile)                trigger (manual click OR auto: now>=next_run_at AND idle)
idle ─────────────────────────► idle ───────────────────────────────────────────► running
  ▲                                                                                   │
  │                                                                                   │ subprocess exits
  │       acknowledge / next poll                                                     ▼
  └────────────────────────────────────────────────── finished (last_outcome set) ◄──┘
```

- **Trigger guard**: `running → running` transition is forbidden; a trigger while
  `running` is rejected (FR-002). Auto-trigger while `running` is skipped, next cycle
  scheduled (FR-010).
- **On any run start**: `next_run_at = run_started_at + interval_seconds` (FR-009).
- **On run end**: `status → finished`, set `last_outcome`/`exit_code`/`run_finished_at`,
  clear `pid`; controller returns to `idle` for scheduling purposes.
- **Boot reconciliation** (FR-013): if loaded `status == running` but no live subprocess
  (pid not alive / fresh process), set `last_outcome = interrupted`, `status → idle`.

### Validation / invariants

- Exactly one run active at a time (`pid` non-null ⇔ `status == running`).
- `percent` is monotonic non-decreasing within a single run.
- `interval_seconds == 0` ⇒ `scheduler_enabled == false` and `next_run_at == null`.
- Controller MUST NOT write any field of `vagas_historico.json` (separation of files).

## Entity: Schedule (derived, not separately persisted)

Conceptual view over RunState for the UI; all fields live in RunState.

- `enabled` ← `scheduler_enabled`
- `interval_seconds` ← `interval_seconds`
- `next_run_at` ← `next_run_at`
- `seconds_remaining` = `next_run_at - now` (computed by the front for the countdown)

# Contract: run_state.json (pipeline ↔ web channel)

The cross-process channel. Path from `RUN_STATE_PATH` (default `run_state.json`, → `./data`
in Docker). Writers use atomic replace (temp file + `os.replace`) to avoid torn reads.

## Who writes what

| Field | Writer | When |
|-------|--------|------|
| `status`, `pid`, `run_started_at`, `run_finished_at`, `trigger`, `last_outcome`, `last_error`, `exit_code`, `next_run_at`, `scheduler_enabled`, `interval_seconds` | web-app controller | on trigger, on run end, on boot reconcile |
| `progress` (`stage`, `percent`, `detail`, `updated_at`) | pipeline subprocess (`main.py`) | at each stage boundary and every N matched jobs |

**Rule**: the pipeline writes ONLY `progress`; the controller owns all lifecycle fields.
To avoid write races on the same file, the pipeline updates progress via a read-merge-
write that touches only the `progress` key (controller does likewise for lifecycle keys).
Single-writer-per-key keeps it safe without a lock.

## Example (mid-run)

```json
{
  "status": "running",
  "pid": 48213,
  "trigger": "auto",
  "run_started_at": "2026-07-05T14:00:02",
  "run_finished_at": null,
  "progress": {
    "stage": "matching",
    "percent": 42,
    "detail": "matching 50/120",
    "updated_at": "2026-07-05T14:02:58"
  },
  "last_outcome": "success",
  "last_error": null,
  "exit_code": null,
  "next_run_at": "2026-07-05T20:00:02",
  "scheduler_enabled": true,
  "interval_seconds": 21600
}
```

## Pipeline emission points (main.py)

Minimal, no logic change — emit progress alongside the existing `logger.info` milestones:

- start scrape → `{stage: "scraping", percent: 0, detail: "coletando vagas"}`
- scrape done → `{stage: "dedupe", percent: 40, detail: "removendo duplicatas"}`
- match loop (every N jobs) → `{stage: "matching", percent: 45 + 50*done/total, detail: "matching {done}/{total}"}`
- report/save → `{stage: "reporting", percent: 95, detail: "gerando relatório"}`
- end → `{stage: "done", percent: 100, detail: "concluído"}`

Emission is best-effort: a failure to write progress MUST NOT abort the pipeline
(Principle VI) — wrap in try/except and continue.

## Staleness

If `status == running` but `progress.updated_at` is older than a threshold AND the `pid`
is not alive, the controller treats the run as `interrupted` on the next boot/reconcile
(FR-013). Live liveness is the `Popen` handle while the app runs.

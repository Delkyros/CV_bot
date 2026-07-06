# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A pipeline that scrapes LinkedIn job postings, filters them by contract type (CLT) and location, scores each against a candidate profile with an LLM, and writes a ranked Markdown report. `main.py` orchestrates; `src/` holds the stages; `webapp.py` is a Flask UI for triaging results.

## Commands

The virtualenv lives in `.venv/` and is the interpreter used for everything. On this Windows machine the Python executable is `.venv/Scripts/python.exe`.

```bash
# Run the full pipeline (scrape -> classify -> match -> report)
.venv/Scripts/python.exe main.py

# Run the web UI (marks jobs Novo/Visto/Inscrito, writes back to history JSON)
.venv/Scripts/python.exe webapp.py           # http://localhost:8000

# Tests (pytest; install with: .venv/Scripts/python.exe -m pip install -r requirements-dev.txt)
.venv/Scripts/python.exe -m pytest -q                          # all
.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -q   # one file
.venv/Scripts/python.exe -m pytest tests/test_scope_filtering.py::<test_name> -v   # one test

# Import smoke test (fast sanity check after refactors)
.venv/Scripts/python.exe -c "import main; from src import scraper, matcher, reporter, settings; print('imports OK')"

# Docker (runs pipeline on a schedule + serves the web UI)
docker compose build && docker compose up -d
```

There is no linter/formatter configured — match the surrounding style.

## Architecture

The pipeline is a linear flow with two LLM-backed decision points. Trace it through `main.py:main()`:

1. **Collect** (`src/scraper.py`) — queries LinkedIn's public Guest API per `(search term × filter)`. Jobs already in the history JSON are excluded *before* downloading the description; location/work-model filtering happens on the search card to save requests. Retries on `429` with rotating User-Agents.
2. **Classify contract** (`src/matcher.py` — `ContractClassifier` / `classify_contract`) — runs during collection, **fully local, no LLM**. CLT vs PJ/freelancer/internship is an explicit-keyword problem, decided by the high-precision regex in `src/text_signals.py` (`strong_non_clt_evidence` + `internship_evidence`). This is a **default-CLT** policy: a job is discarded *only* on an explicit non-CLT signal (contractor/PJ/USD-hourly or internship); anything else is kept as assumed CLT.
3. **Dedupe** (`main.py:dedupe_jobs`) — collapses reposts (same title+company, different IDs/cities) before spending LLM match calls.
4. **Match & scope-filter** (`main.py:analyze_and_filter_jobs` → `src/matcher.py:analyze_match`) — first a deterministic `out_of_scope_title()` gate drops clearly off-track titles with no LLM call; the rest get an LLM `{match_score, strengths, gaps, verdict, core_role_compatible}`. This is a **default-in-scope** policy: a job is dropped as out-of-scope only on an *explicit* `core_role_compatible: false`.
5. **Report & remember** (`src/reporter.py:generate_report`, `main.py:save_job_history`) — the report shows only jobs passing `passes_relevance_filter` (CLT score ≥ `REPORT_MIN_CLT_SCORE` 0.7 **and** match ≥ `REPORT_MIN_MATCH_SCORE` 70); the history JSON persists **all** in-scope jobs (sub-bar ones flagged `status="irrelevant"`) so they are never re-scraped.

### LLM provider chain (`src/matcher.py`)
`_complete_with_providers()` tries providers in `PROVIDER_ORDER = ("openrouter", "gemini")`, cycling up to `LLM_MAX_PROVIDER_CYCLES` on quota/`429` errors. OpenRouter (free models) is primary, Gemini the fallback. Cycles/waits are kept low so a saturated free pool fails fast instead of sleeping minutes per job. If every provider fails (or none is configured), `main.py:_fallback_analysis` calls `src/local_match.py`, which scores the job **locally** by mean embedding proximity between the profile and the job's chunks (chonkie + a small offline model2vec model) — so the run never crashes and a job is never discarded on infrastructure failure.

### State: `vagas_historico.json`
Single source of truth shared by the pipeline and the web UI. **The pipeline and web UI both write it.** `save_job_history` reloads from disk at save time and preserves the user-owned fields (`USER_STATUS_FIELDS` in `main.py`: status, notes, status_updated_at, error_class) so a scrape running while you triage never clobbers your marks. When editing history-writing code, preserve this merge-don't-overwrite invariant.

### Run control (`src/run_controller.py`)
`webapp.py` can trigger and observe the pipeline. `RunController` runs `python main.py` as a **subprocess** (isolates `sys.exit`/crashes from the web server), enforces a single-run lock (a 2nd trigger → HTTP 409), and owns a recurring **scheduler** thread (`RUN_INTERVAL_SECONDS`, `SCHEDULER_ENABLED`, `RUN_ON_START`). Endpoints: `POST /api/run` (trigger), `GET /api/run/status` (state + progress + countdown). State lives in **`run_state.json`** (`RUN_STATE_PATH`), a file **separate** from the history — the controller NEVER writes `vagas_historico.json` (Principle III). Two writers share `run_state.json` by key: the controller owns lifecycle keys, the pipeline (`main.py` via `emit_progress`) owns only `progress`; the controller writes the initial state *before* spawning so the subprocess's progress is never clobbered. Single-run safety is the in-memory guard (one web process is the only trigger source — the button and the in-app scheduler both route through `start_run`'s lock), so no cross-process lock is needed. In Docker a single `web` service owns scheduling and runs the pipeline (no separate scraper container). Spec: `specs/001-pipeline-run-control/`.

## Conventions

- **Config keys and profile fields are Portuguese on purpose** (`termos_busca`, `perfil_candidato`, `filtros_busca`, skill categories like `ia_e_llms`). `config/keywords.yaml` is not translated and is git-ignored (personal data); `config/keywords.example.yaml` is the versioned template. Code/comments/logs are English.
- **Everything operational is env-configurable** via `src/settings.py` helpers (`env_str/float/int/bool/list`), read from `.env`. Nothing is hardcoded; every tunable has a default. See the README's Tunables table before adding a new constant.
- **The two "default-keep" policies are load-bearing** (default-CLT in classification, default-in-scope in matching). Preserve them when touching `matcher.py` or the filter logic — the point is to never silently drop a good job. `_coerce_bool` defaults to `True` for this reason.
- File paths (`HISTORY_PATH`, `REPORT_OUTPUT_PATH`, `KEYWORDS_CONFIG_PATH`) come from env; under Docker they point into `./data/`.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A pipeline that scrapes LinkedIn job postings, filters them by contract type (CLT) and location, and scores each against a candidate profile. `main.py` orchestrates; `src/` holds the stages; `webapp.py` is a Flask UI for triaging results (with a "Relatório" tab). The old Markdown report was removed — the web app reading the history JSON is now the only output surface.

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
2. **Classify contract** (`src/matcher.py` — `classify_contract`) — runs during collection, **fully local, no LLM**. CLT vs PJ/freelancer/internship is an explicit-keyword problem, decided by the high-precision regex in `src/text_signals.py` (`strong_non_clt_evidence` + `internship_evidence`). This is a **default-CLT** policy: a job is discarded *only* on an explicit non-CLT signal (contractor/PJ/USD-hourly or internship); anything else is kept as assumed CLT.
3. **Dedupe** (`main.py:dedupe_jobs`) — collapses reposts (same title+company, different IDs/cities) before spending LLM match calls.
4. **Match & scope-filter** (`main.py:analyze_and_filter_jobs`) — three scope gates run before spending an LLM call: (a) the curated `out_of_scope_title()` regex, (b) the learned blocklist of titles you flagged "Escopo incorreto", and (c) the **embedding scope gate** — drops a job only when BOTH its title is far from every `termos_busca` role (`score_title`) AND its description is far from the CV (`score_vetor_desc`); thresholds `SCOPE_TITLE_MIN`/`SCOPE_DESC_MIN` (both 0.40, raw cosine), skipped when no search terms are passed. Survivors get an LLM `{match_score, strengths, gaps, verdict, core_role_compatible}` (Gemini only — see chain below). Each kept job carries **three comparable scores** — `score_gemini` (LLM 0-100), `score_vetor_desc`, `score_title` (both cosines) — surfaced in the web app's Relatório tab to judge whether the LLM is worth keeping. **default-in-scope** throughout: a job is dropped only on explicit/dual-weak evidence, never silently.
5. **Remember** (`main.py:save_job_history`) — persists **all** in-scope jobs to the history JSON (the three scores + `strengths`/`gaps`/`verdict` for the Relatório tab), sub-bar ones flagged `status="irrelevant"` so they are never re-scraped. `passes_relevance_filter` (CLT ≥ `REPORT_MIN_CLT_SCORE` 0.7 **and** match ≥ `REPORT_MIN_MATCH_SCORE` 70) now only drives the `relevant` flag the web UI filters on.

### LLM provider (`src/matcher.py`)
`_gemini_complete()` calls Gemini — the only provider — retrying up to `LLM_MAX_PROVIDER_CYCLES` on quota/`429` errors. The whole OpenRouter integration (free-model list, auto-discovery, HTTP call, provider-chain registry) was deleted after its scores proved garbage (84% of user-rejected jobs got 100) and lives only in git history. If Gemini fails (or no key is configured), `main.py:_fallback_analysis` calls `src/local_match.py`, which scores the job **locally** by mean embedding proximity between the profile and the job's chunks (chonkie + a multilingual model2vec model, `LOCAL_MATCH_MODEL`) — so the run never crashes and a job is never discarded on infrastructure failure. `local_match.py` also exposes `description_similarity` / `title_scope_similarity`, the raw cosines behind `score_vetor_desc` / `score_title` and the embedding scope gate.

### State: `vagas_historico.json`
Single source of truth shared by the pipeline and the web UI. **The pipeline and web UI both write it.** `save_job_history` reloads from disk at save time and preserves the user-owned fields (`USER_STATUS_FIELDS` in `main.py`: status, notes, status_updated_at, error_class) so a scrape running while you triage never clobbers your marks. When editing history-writing code, preserve this merge-don't-overwrite invariant.

### Run control (`src/run_controller.py`)
`webapp.py` can trigger and observe the pipeline. `RunController` runs `python main.py` as a **subprocess** (isolates `sys.exit`/crashes from the web server), enforces a single-run lock (a 2nd trigger → HTTP 409), and owns a recurring **scheduler** thread (`RUN_INTERVAL_SECONDS`, `SCHEDULER_ENABLED`, `RUN_ON_START`). Endpoints: `POST /api/run` (trigger), `GET /api/run/status` (state + progress + countdown). State lives in **`run_state.json`** (`RUN_STATE_PATH`), a file **separate** from the history — the controller NEVER writes `vagas_historico.json` (Principle III). Two writers share `run_state.json` by key: the controller owns lifecycle keys, the pipeline (`main.py` via `emit_progress`) owns only `progress`; the controller writes the initial state *before* spawning so the subprocess's progress is never clobbered. Single-run safety is the in-memory guard (one web process is the only trigger source — the button and the in-app scheduler both route through `start_run`'s lock), so no cross-process lock is needed. In Docker a single `web` service owns scheduling and runs the pipeline (no separate scraper container). Spec: `specs/001-pipeline-run-control/`.

## Conventions

- **Config keys and profile fields are Portuguese on purpose** (`termos_busca`, `perfil_candidato`, `filtros_busca`, skill categories like `ia_e_llms`). `config/keywords.yaml` is not translated and is git-ignored (personal data); `config/keywords.example.yaml` is the versioned template. Code/comments/logs are English.
- **Everything operational is env-configurable** via `src/settings.py` helpers (`env_str/float/int/bool/list`), read from `.env`. Nothing is hardcoded; every tunable has a default. See the README's Tunables table before adding a new constant.
- **The two "default-keep" policies are load-bearing** (default-CLT in classification, default-in-scope in matching). Preserve them when touching `matcher.py` or the filter logic — the point is to never silently drop a good job. `_coerce_bool` defaults to `True` for this reason.
- File paths (`HISTORY_PATH`, `KEYWORDS_CONFIG_PATH`) come from env; under Docker they point into `./data/`.

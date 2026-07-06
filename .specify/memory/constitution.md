<!--
SYNC IMPACT REPORT
==================
Version change: (template) → 1.0.0
Rationale: Initial ratification. First concrete constitution for CV_bot, encoding
the load-bearing policies already present in the codebase (CLAUDE.md + source).
No prior versioned principles existed, so this is a MAJOR baseline (1.0.0).

Principles defined:
  I.   Default-CLT Classification
  II.  Default-In-Scope Matching
  III. Merge-Don't-Overwrite State
  IV.  Everything Env-Configurable
  V.   Portuguese Config, English Code
  VI.  Never-Crash Resilience
  VII. Self-Adjusting Scope Filter

Added sections:
  - Data & Privacy Constraints (Section 2)
  - Development Workflow & Quality Gates (Section 3)
  - Governance

Templates reviewed:
  ✅ .specify/templates/plan-template.md  — Constitution Check gate reads this file
       dynamically; no hardcoded principle names to update.
  ✅ .specify/templates/spec-template.md  — no principle-driven mandatory sections
       to add/remove.
  ✅ .specify/templates/tasks-template.md — task categories unaffected by these
       (mostly data-policy) principles.

Deferred TODOs:
  - RATIFICATION_DATE set to the constitution's first-write date (2026-07-05); the
    project itself predates this document. Adjust if an earlier formal adoption date
    is desired.
-->

# CV_bot Constitution

CV_bot is a pipeline that scrapes LinkedIn job postings, filters them by contract
type (CLT) and location, scores each posting against a candidate profile with an LLM,
and writes a ranked Markdown report. A Flask web UI (`webapp.py`) triages results.
This constitution records the non-negotiable policies that keep the pipeline honest:
its purpose is to surface good jobs and never silently lose one.

## Core Principles

### I. Default-CLT Classification

Contract classification is a **default-CLT** policy. A job MUST be discarded as
non-CLT only on an **explicit** non-CLT signal (PJ/contractor, USD-hourly, or
internship). When evidence is absent or ambiguous, the job is kept as assumed CLT.
Classification MUST run fully locally (no LLM), decided by the high-precision regex
in `src/text_signals.py` (`strong_non_clt_evidence` + `internship_evidence`).

Rationale: CLT-vs-not is an explicit-keyword problem; a local high-precision gate is
cheaper and more predictable than an LLM, and defaulting to keep prevents throwing
away a good CLT job on missing text.

### II. Default-In-Scope Matching

Scope filtering is a **default-in-scope** policy. A job MUST be dropped as
out-of-scope only on (a) the deterministic `out_of_scope_title()` title gate, (b) a
learned "Escopo incorreto" mark (Principle VII), or (c) an **explicit**
`core_role_compatible: false` from the LLM. `_coerce_bool` MUST default to `True` so a
malformed or missing LLM field never drops a job. Never silently drop a good job.

Rationale: The candidate is better served by a false positive they can reject in the
UI than by a false negative they never see. Explicit-only drops make the filter
auditable.

### III. Merge-Don't-Overwrite State

`vagas_historico.json` is the single source of truth, written by **both** the pipeline
and the web UI. `save_job_history` MUST reload from disk at save time and preserve the
user-owned fields (`USER_STATUS_FIELDS`: `status`, `notes`, `status_updated_at`,
`error_class`). A scrape running while the user triages MUST NEVER clobber their marks.

Rationale: Two writers share one file; last-writer-wins would destroy human triage
work. Merge-on-save is the invariant that makes concurrent scrape + triage safe.

### IV. Everything Env-Configurable

Every operational tunable MUST be configurable via environment variables through the
`src/settings.py` helpers (`env_str/float/int/bool/list`), read from `.env`. Nothing
operational is hardcoded, and every tunable MUST have a sensible default so the
pipeline runs with an empty `.env`. New constants are documented in the README's
Tunables table before use.

Rationale: The pipeline runs in different environments (local, Docker); env-driven
config with defaults keeps deployment friction and hidden magic numbers to zero.

### V. Portuguese Config, English Code

Config keys and profile fields are Portuguese **on purpose** (`termos_busca`,
`perfil_candidato`, `filtros_busca`, skill categories). Code, comments, and logs are
English. `config/keywords.yaml` is git-ignored (personal data); the versioned template
is `config/keywords.example.yaml` and MUST stay in sync structurally.

Rationale: The domain (Brazilian job market, candidate-facing config) is Portuguese;
the engineering is English. Mixing is deliberate, not accidental — do not "translate"
config keys.

### VI. Never-Crash Resilience

The LLM match MUST degrade gracefully. `_complete_with_providers()` cycles the
provider chain (`PROVIDER_ORDER = openrouter → gemini`) on quota/`429`, and when every
provider fails or none is configured, `_fallback_analysis` MUST score the job locally
via embedding proximity (`src/local_match.py`). A run MUST NEVER crash, and a job MUST
NEVER be discarded because of an infrastructure failure.

Rationale: Free LLM pools are flaky; infra failure is not a signal about a job's fit.
The offline embedding fallback keeps runs complete and decisions job-driven.

### VII. Self-Adjusting Scope Filter

User "Escopo incorreto" marks in the web UI MUST feed back into a deterministic
blocklist (`learned_scope_blocklist`) so a rejected role is dropped on sight next run
— no LLM call, no hand-written regex. **Known limitation (documented, not aspirational):**
the blocklist currently matches the exact normalized title key and does not generalize
to variants (different city/UF/seniority suffix). Improvements to generalization MUST
preserve the deterministic, learn-from-marks behavior.

Rationale: The user's own corrections are the highest-signal scope data. Capturing
the limitation here prevents mistaking a near-duplicate that slips through for a
regression of the mechanism.

## Data & Privacy Constraints

- **Personal data stays out of git.** `config/keywords.yaml` (candidate profile,
  search terms) is git-ignored. Only the redacted `keywords.example.yaml` is versioned.
- **History is the anti-rescrape ledger.** Every in-scope job is persisted (sub-bar
  ones flagged `status="irrelevant"`) so it is never re-scraped; the report shows only
  jobs passing `passes_relevance_filter`.
- **Scraping is polite.** The LinkedIn Guest API is queried with location/work-model
  filtering done on the search card (before downloading descriptions), rotating
  User-Agents, and `429` retries — to minimize requests and avoid blocks.

## Development Workflow & Quality Gates

- **Tests:** `pytest` (`.venv/Scripts/python.exe -m pytest -q`). Changes to the
  filters (contract, scope, relevance) MUST keep `tests/test_scope_filtering.py` and
  `tests/test_pipeline.py` green, and add a case for any new drop/keep behavior.
- **Import smoke test** after refactors:
  `.venv/Scripts/python.exe -c "import main; from src import scraper, matcher, reporter, settings"`.
- **Style:** no linter/formatter is configured — match the surrounding style.
- **Invariant protection:** any change touching `matcher.py`, the filter logic, or
  history writing MUST explicitly preserve Principles I, II, and III.

## Governance

This constitution documents the load-bearing behavior of CV_bot; it supersedes ad-hoc
practice where they conflict. Amendments are made by editing this file with:

- **Versioning (semver):** MAJOR = a principle removed or redefined incompatibly;
  MINOR = a principle/section added or materially expanded; PATCH = clarifications and
  wording. The version line below MUST be bumped in the same change.
- **Amendment procedure:** update the principle(s), bump the version, refresh the Sync
  Impact Report comment at the top, and confirm the dependent `.specify/templates/*`
  still align.
- **Compliance:** code changes that weaken a "MUST" MUST either be reverted or
  accompanied by a constitution amendment justifying the change. `CLAUDE.md` remains
  the day-to-day runtime guidance and MUST stay consistent with these principles.

**Version**: 1.0.0 | **Ratified**: 2026-07-05 | **Last Amended**: 2026-07-05

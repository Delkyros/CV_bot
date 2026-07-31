# Implementation Plan: Work-model accuracy — mitigate LinkedIn's leaky remote filter

**Branch**: `feat/generalize-config` (shipped here; the feature dir is `004-workplace-model-accuracy`) | **Date**: 2026-07-31 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/004-workplace-model-accuracy/spec.md`

**Status**: Reconstructed from the shipped code. This plan documents the design that
**was** built, not one proposed beforehand — the feature began as an analysis request and
the code landed as each measurement confirmed a cause. Every file, symbol and number below
was read back out of the working tree, so it describes reality. Where the built design has
a wart the plan says so rather than tidying it retroactively.

## Summary

LinkedIn's `f_WT=2` (remote) search filter is silently dropped in ~3 of 24 requests, and
**no per-job work-model field is reachable without authentication** (four surfaces tested,
all negative — `research.md` §2). So the leak cannot be fixed, only mitigated with text and
historical signals available to an anonymous client.

Five changes, ordered by confidence, each independently useful:

1. **Title gate** — reuse the existing remote-conflict helper on the card **title**, before
   the description download. Highest-confidence, zero-request signal.
2. **Metro-area fix** — strip LinkedIn's `"e Região"` / `"Grande "` qualifier before the
   whole-component hub match. Repairs a live feature-003 regression (its FR-006).
3. **`distance` passthrough** — expose LinkedIn's search radius per search filter.
4. **Location-shape risk label** — classify a remote job's location against the searched
   country and surface it in the Relatório tab. **Ranking only, never a gate.**
5. **Learned location blocklist + persisted evidence** — skip repeat-offender companies the
   user never engaged with, and record the work-model evidence that made the description
   guard unmeasurable.

The enabling insight is that (1) needed **no new logic**: `description_conflicts_with_remote`
already implemented exactly the right decision and was simply never called with the title.
The change was to rename it `conflicts_with_remote` and add one call site — not to write a
second, drifting copy.

## Technical Context

**Language/Version**: Python 3.11 (`.venv/Scripts/python.exe`)

**Primary Dependencies**: stdlib `re`, `collections`; existing `bs4`, `requests`,
`src/settings.py` env helpers. **No new dependency.**

**Storage**: `vagas_historico.json` gains two pipeline-owned fields per entry
(`location_shape`, `workplace_evidence`); `config/keywords.yaml` gains a per-filter
`distancia` key; `.env` gains `LOCATION_BLOCKLIST_MIN_ERRORS`.

**Testing**: pytest — `tests/test_pipeline.py` only. 229 tests total after this feature
(was 237). The count went **down** on the final pass: 28 parametrized title fixtures were
replaced with 7 after measuring that the 28 exercised only **5 distinct decision paths**. The
remaining fixtures are still **verbatim real titles** from the user's history, so the tests
assert against observed failures rather than invented strings; the full offender list lives in
`spec.md` / `research.md`, which is where data belongs — not archived as duplicate assertions.

**Target Platform**: Local Windows / Docker Linux — unchanged.

**Project Type**: Single project (pipeline + Flask UI). No new component.

**Performance Goals**: Net **reduction** in requests and LLM calls — the title gate and the
company blocklist both drop cards *before* the description download. The two new persisted
fields are computed from strings already in memory.

**Constraints**:
- No gate may fire on a *missing* signal (Constitution II). Every new drop requires explicit
  positive evidence.
- The location-shape signal MUST NOT filter: measured precision is 12.4%, so a hard drop
  costs ~7 good jobs per bad one.
- Authenticated scraping is out of scope (spec FR-013).
- Existing filter tests MUST stay green (feature 003 FR-006).

**Scale/Scope**: 4 source files (`src/scraper.py`, `main.py`, `webapp.py`,
`web/index.html`), 2 config/doc files, 1 test file.

## Constitution Check

*GATE: re-checked against the shipped code, not the intent.*

| Principle | Impact | Verdict |
|-----------|--------|---------|
| I. Default-CLT | Contract classification untouched. | ✅ N/A |
| II. Default-In-Scope | Three new drop paths (title gate, company blocklist, existing description guard) all require **explicit** positive evidence; the location-shape signal deliberately drops nothing. `remote_conflict_evidence` returning empty lists ⇒ keep. | ✅ Preserved |
| III. Merge-Don't-Overwrite | Two new fields are pipeline-owned, written in the same `entry` dict as the score fields; `USER_STATUS_FIELDS` untouched, so `status`/`notes`/`status_updated_at`/`error_class` still survive a concurrent triage. | ✅ Preserved |
| IV. Everything Env-Configurable | `LOCATION_BLOCKLIST_MIN_ERRORS` via `env_int` with a default and a README row. `distancia` follows the `geo_id` precedent (raw LinkedIn parameter → YAML, not `.env`) because it is **per search filter**, not global. | ✅ Compliant |
| V. Portuguese Config, English Code | New config key `distancia` is Portuguese; all code, comments and field values (`country`/`city_state`) are English, with the Portuguese risk labels mapped in the UI layer. | ✅ Compliant |
| VI. Never-Crash Resilience | `remote_location_shape` returns `None` on missing input; `remote_conflict_evidence` returns `([], [])` on empty text; unparseable locations fall through to the pessimistic `city_state` bucket rather than raising. | ✅ Preserved |
| VII. Self-Adjusting Scope Filter | `learned_location_blocklist` is a deliberate sibling of `learned_scope_blocklist` — same learn-from-your-marks shape, rebuilt from history each run, never persisted. | ✅ Extended |

**No violations. No complexity deviations to justify** — see Complexity Tracking.

## Project Structure

### Documentation (this feature)

```
specs/004-workplace-model-accuracy/
├── spec.md          # 5 user stories, 13 FRs, 9 measured success criteria
├── research.md      # API probing: what LinkedIn does and does not expose, and how it was verified
├── data-model.md    # the two new fields, the config key, the env var
├── plan.md          # this file
└── tasks.md         # retrospective task breakdown, mapped to shipped code
```

No `contracts/` directory: this feature adds no HTTP endpoint and no new artifact format.
The one API-surface change (`location_shape` in the `/api/jobs` payload) is a single
additive field, documented in `data-model.md`.

### Source Code (repository root)

```
src/scraper.py                 # the bulk of the feature
├── _METRO_AREA_QUALIFIER      # NEW  regex stripping "e Região" / "Grande " / "Região Metropolitana de "
├── workplace_matches          # CHANGED  strips the metro qualifier before the whole-component hub match
├── conflicts_with_remote      # RENAMED from description_conflicts_with_remote; now delegates
├── remote_conflict_evidence   # NEW  (onsite_hits, remote_hits) — separates "silent" from "vetoed"
├── remote_location_shape      # NEW  country | city_country | metro | city_state
└── scrape_linkedin_jobs       # CHANGED  +distance, +excluded_companies params;
                               #          title gate before download; records both new fields

main.py
├── LOCATION_ERROR_CLASS       # NEW  "Localidade/Modelo incorreto" (mirrors webapp.ERROR_CLASSES)
├── ENGAGED_STATUSES           # NEW  ("viewed", "applied") — the veto set
├── location_blocklist_min_errors  # NEW  env_int("LOCATION_BLOCKLIST_MIN_ERRORS", 2)
├── learned_location_blocklist # NEW  repeat offenders, engagement-vetoed
├── main()                     # CHANGED  builds the blocklist, reads `distancia`, passes both through
└── save_job_history           # CHANGED  persists location_shape + workplace_evidence

webapp.py                      # CHANGED  location_shape added to the job payload
web/index.html                 # CHANGED  "Local·risco" column, SHAPE_LABEL/SHAPE_RANK, CSS, colspan 4→5
config/keywords.yaml           # CHANGED  distancia: 25 on the four hybrid filters (git-ignored)
config/keywords.example.yaml   # CHANGED  documented `distancia` on the example hybrid filter
README.md                      # CHANGED  Tunables row for LOCATION_BLOCKLIST_MIN_ERRORS
tests/test_pipeline.py         # CHANGED  +13 tests, then -21 redundant cases (237 → 250 → 229)
```

## Key design decisions (as built)

**Reuse over a second implementation.** The title gate calls the same helper as the
description guard. A separate title-specific predicate would have drifted the two notions of
"declares hybrid with no remote option" apart. Cost: renaming a function referenced by two
existing tests. Worth it.

**The veto applies per-text, not globally.** `conflicts_with_remote(title)` and
`conflicts_with_remote(description)` are independent calls. This is the whole point: a stray
"home office" in a benefits list vetoes the *description* check, and must not be allowed to
veto the *title* check as well. Measured: 0 of the 219 flagged jobs would have been caught by
the description guard as historically configured.

**Strip-then-match, not add-more-patterns, for metro areas.** `_METRO_AREA_QUALIFIER.sub("")`
normalizes `"florianopolis e regiao"` → `"florianopolis"` and leaves
`"sao jose dos campos"` untouched, so feature 003's homonym rejection survives unchanged. The
alternative — adding `"<city> e regiao"` variants to the hub list — would have multiplied the
config surface by the number of qualifier spellings.

**Country inferred, not tabulated.** `remote_location_shape` takes the country as the last
comma-component of the *searched* location, exploiting LinkedIn's own
"CITY, STATE, COUNTRY" convention. No country table, so feature 003's generalization holds:
verified with Portugal in `test_remote_location_shape_uses_the_configured_country`.

**Risk rank in the UI layer, not the data.** `location_shape` is persisted as a stable
enum; the measured percentages and the Portuguese labels live in `web/index.html`
(`SHAPE_LABEL`, `SHAPE_RANK`). Sorting maps to measured risk because alphabetical order on
the enum (`city_country < city_state < country < metro`) is meaningless.

**Counts, not text, for evidence.** `workplace_evidence` stores four integers rather than
the matched patterns or the description. Keeps the history small, avoids persisting large
third-party text, and is sufficient to identify the vetoed case.

**Threshold pinned at 2 with a documented reason.** `LOCATION_BLOCKLIST_MIN_ERRORS=1` is
technically allowed but the README explains why not: 80% of offending companies offend
exactly once, so `1` adds ~112 companies of blast radius to catch errors it could not have
predicted.

## Complexity Tracking

No constitutional violations to justify. Two deliberate simplifications instead:

| Simplification | Ceiling | Upgrade path |
|----------------|---------|--------------|
| Location shape is a 4-bucket enum from string shape, not a learned model | Correlational — the guest card shows LinkedIn's display normalization, not the posted `location` field | The persisted `workplace_evidence` + `location_shape` are exactly the features a fused model would need; `train_meta_model.py` is the natural home once enough marks accumulate |
| Company blocklist keyed on exact normalized company name | Misses spelling variants of the same employer ("BIX Tecnologia" vs "BIX") | Fuzzy/canonical company keying, only if the data shows it matters |

## What was explicitly not built

- **Authenticated fetch of the work-model tag** — the only thing that would actually close
  the gap. Out of scope (spec FR-013): ToS violation and account risk.
- **Location shape as a filter** — measured and rejected: 12.4% precision, 255 engaged jobs
  in the drop set.
- **Collapsing the four hub searches into one wide-radius search** — tested, loses 6 of 16
  jobs (`research.md` §4).
- **Dedupe of reposts against the whole history** — proposed during analysis and **rejected
  by the maintainer**; LinkedIn's documented `alternateLocations` (max 7) means the
  multi-city copies are one posting, so blocking them would discard legitimate alternate
  locations of an untriaged job.
- **Persisting full descriptions** — considered for measurability, rejected in favour of the
  much smaller evidence counts.

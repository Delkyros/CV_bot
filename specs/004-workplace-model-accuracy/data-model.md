# Phase 1 Data Model: work-model diagnostics

No database. Two new fields on each `vagas_historico.json` entry, one new per-filter config
key, one new environment variable.

## New fields on a history entry

Written by `save_job_history` (`main.py`) from the job dict the scraper produces. Both are
**diagnostics**: no gate reads them back, so a missing or stale value can never drop a job
(Constitution Principle II).

| Field | Type | Values | Produced by |
|-------|------|--------|-------------|
| `location_shape` | string \| null | `country` \| `city_country` \| `metro` \| `city_state` | `scraper.remote_location_shape(loc, search_location)` |
| `workplace_evidence` | object | `{title_onsite, title_remote, desc_onsite, desc_remote}` — ints | `scraper.remote_conflict_evidence` on the title and on the description |

`null` / absent for jobs from a **non-remote** search: neither signal means anything when the
search itself asked for hybrid or on-site.

### `location_shape`

Derived from the job's location against the **searched country**, taken as the last
comma-component of the filter's `localizacao`. No country table — so it generalizes to any
region (feature 003 / Principle IV) and works for both a country search (`"Brasil"`) and a
city search (`"São José, Santa Catarina, Brasil"`).

| Value | Shape | Measured wrong-rate | n |
|-------|-------|--------------------:|--:|
| `country` | single component == country (`Brasil`) | 1.4% | 1645 |
| `metro` | single component with a metro qualifier (`São Paulo e Região`) | 6.0% | 319 |
| `city_country` | multi-component ending in the country (`São Paulo, Brasil`) | 6.8% | 148 |
| `city_state` | anything else (`São Paulo, SP`) | 13.5% | 1236 |

Rates are the share of records the user hand-flagged `Localidade/Modelo incorreto`, over the
3,348 remote records of the 2026-07 history. They are **displayed in the UI**, so they are
data with a provenance, not constants: re-measure if the region or the user changes.

`city_state` is the catch-all, i.e. the pessimistic bucket, so unparseable input degrades to
"flagged as risky" rather than "silently trusted".

### `workplace_evidence`

Counts, not the matched patterns or the description text — the history stays small and no
large third-party text is persisted.

| Combination | Meaning |
|-------------|---------|
| `desc_onsite == 0` | the description said nothing about the work model |
| `desc_onsite > 0 and desc_remote > 0` | **the vetoed case** — on-site declared, but a remote word (often a stray "home office" benefit) suppressed the guard. The leading suspect for a job the user will later flag. |
| `desc_onsite > 0 and desc_remote == 0` | cannot appear on a persisted remote-search job — the job was dropped at collection |
| `title_onsite > 0 and title_remote == 0` | likewise cannot appear (dropped by the title gate) |

This exists because the previous boolean-only helper made the first and second rows
indistinguishable, which is why the description guard's effectiveness could not be measured
at all (see `research.md` §8).

### Merge behaviour

Both fields are pipeline-owned and rewritten each run, like the score fields. They are **not**
in `USER_STATUS_FIELDS`, so the merge-don't-overwrite invariant on `status` / `notes` /
`status_updated_at` / `error_class` is untouched (Principle III).

Entries written before this feature simply lack both keys; readers treat absent as `null`.

## New per-filter config key

In `config/keywords.yaml`, inside each entry of `filtros_busca` (alongside `geo_id`):

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `distancia` | int \| absent | absent | LinkedIn's `distance` — search radius in **miles** around `geo_id`. Absent ⇒ parameter omitted from the URL ⇒ LinkedIn's own default. `0` is a real radius (returns nothing) and is sent as such. |

Per-filter rather than global because each filter has its own `geo_id`, and it is meaningless
on the country-level geoId the remote filter uses (`research.md` §4). Follows the `geo_id`
precedent: a raw LinkedIn parameter lives in the YAML, not in `.env`.

## New environment variable

| Var | Helper | Default | Consumed by |
|-----|--------|---------|-------------|
| `LOCATION_BLOCKLIST_MIN_ERRORS` | `env_int` | `2` | `main.learned_location_blocklist` |

Read at run time, documented in the README Tunables table with the measured warning against
setting it to `1` (80% of offending companies offend exactly once, so `1` adds ~112 companies
of blast radius for errors it could not have predicted).

## Derived, not persisted

- **Learned location blocklist** — a set of normalized company names rebuilt from the history
  on every run, exactly like `learned_scope_blocklist` (Principle VII). Never stored, so a
  company leaves it automatically once the user marks any of its jobs `viewed`/`applied`.

# Feature Specification: Generalize the pipeline for any profile & any search

**Feature Branch**: `003-generalize-config`

**Created**: 2026-07-24

**Status**: Draft

**Input**: User description: "Generalizar a aplicação para qualquer perfil de candidato e qualquer busca de vaga (localização, cidades-hub do modelo híbrido, países aceitos no remoto, escopo de cargos, idioma). Hoje há hardcodes acoplados a UM usuário (Data Scientist da Grande Florianópolis). Tudo deve ser configurável por variável de ambiente, de forma clara e documentada, com defaults que preservam o comportamento atual."

## Context

Today the pipeline runs correctly only for **one** user: a senior Data Scientist / ML
Engineer hunting jobs in the Grande Florianópolis region. Four things are hardcoded to
that single user and cannot be changed without editing source code:

1. **Hybrid hub cities** — `scraper.workplace_matches` only accepts hybrid jobs in
   Florianópolis / São José-SC / Palhoça / Biguaçu.
2. **Job-title scope blocklist** — `text_signals.out_of_scope_title` is a hand-curated
   regex list encoding "off-track for a DS/ML candidate" (drops data analyst, BI,
   backend, QA, HR, sales, etc.). For any other target profession it drops the *right*
   jobs.
3. **Remote foreign-country blocklist** — the list of countries rejected for remote
   roles is inline in `scraper.workplace_matches`.
4. **LLM output language** — the match prompt forces strengths/gaps/verdict to be
   written in Portuguese.

This feature moves all four behind clearly named, documented environment variables,
with defaults that reproduce today's behavior exactly, so **no data specific to the
current user stays in the code**.

## Clarifications

### Session 2026-07-24

- Q: Where does the seed for the title scope blocklist live? → A: In `config/keywords.yaml`
  as a structured list (human-readable reason → regex), with the current DS/ML set as the
  versioned default in `config/keywords.example.yaml`. At runtime `out_of_scope_title`
  applies the deduplicated union of this YAML seed and the local learned file.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Search for a different profession (Priority: P1)

A user whose target role is not a Data Scientist (e.g. a Backend Developer, a Data
Analyst, a Nurse, an Accountant) configures their `termos_busca` and profile and runs
the pipeline. The title scope filter must not silently drop the roles that are exactly
what they are looking for.

**Why this priority**: The title scope blocklist is the most damaging hardcode — for
any non-DS profile it deletes the correct jobs before an LLM ever sees them, and there
is no env or config to change it today. Without this, the app is unusable for anyone
but the current user.

**Independent Test**: Configure search terms for a non-DS role, run the scope gate over
a set of matching titles, and confirm none are dropped by the built-in blocklist that
would have dropped them before this change.

**Acceptance Scenarios**:

1. **Given** the profile-specific scope blocklist is left at its default, **When** the
   pipeline runs, **Then** the drop/keep decisions are identical to today's for the
   current DS/ML user (no regression).
2. **Given** a user targeting a role the default blocklist would reject (e.g. "Analista
   de Dados"), **When** they adjust the scope configuration for their profession, **Then**
   those titles are no longer hard-dropped and reach the LLM/embedding gates.
3. **Given** a user who does not want any hand-written title blocklist, **When** they
   disable it via configuration, **Then** the pipeline relies only on the learned
   blocklist, the embedding scope gate, and the LLM — and never crashes.

---

### User Story 2 - Search a different location and work model (Priority: P1)

A user in another city/region configures their hybrid hub cities and the countries
accepted for remote roles, and the pipeline honors those instead of the Grande
Florianópolis defaults.

**Why this priority**: Location and work-model are core search parameters. Hybrid search
currently returns zero results outside Florianópolis regardless of the configured
`geo_id`, which makes the feature silently broken for other regions.

**Independent Test**: Set the hybrid hub cities to a different city list, feed the
work-model filter locations from that city and from elsewhere, and confirm only the
configured city's jobs pass.

**Acceptance Scenarios**:

1. **Given** the hub cities are left at their default, **When** a hybrid job in
   Florianópolis / São José-SC / Palhoça / Biguaçu is evaluated, **Then** it passes and
   the São Paulo homonyms ("São José dos Campos/do Rio Preto") are still rejected — same
   as today.
2. **Given** the hub cities are set to a different city, **When** a hybrid job in that
   city is evaluated, **Then** it passes; a hybrid job in Florianópolis is rejected.
3. **Given** the remote accepted-region configuration is left at its default, **When** a
   remote job located outside Brazil is evaluated, **Then** it is rejected — same as
   today; a Brazilian-city remote job passes.
4. **Given** a user sets a different remote-region policy, **When** remote jobs are
   evaluated, **Then** the new policy decides which locations pass.

---

### User Story 3 - Read results in the user's language (Priority: P2)

A user who is not Portuguese-speaking configures the language in which the LLM writes
the strengths, gaps, and verdict, and reads the report in that language.

**Why this priority**: Improves usability for non-Portuguese users but does not block
the pipeline from producing correct scores; the analysis itself is language-agnostic.

**Independent Test**: Set the output language to English, run a match, and confirm the
prompt instructs the model to answer in English while the score is unchanged.

**Acceptance Scenarios**:

1. **Given** the output language is left at its default, **When** a match runs, **Then**
   the model is instructed to write strengths/gaps/verdict in Portuguese — same as today.
2. **Given** the output language is set to another language, **When** a match runs,
   **Then** the model is instructed to answer in that language.

---

### User Story 4 - Discover and understand every knob (Priority: P2)

A new user reads one documented place (the README Tunables table + the example config)
and understands every environment variable that controls profile/search/location/work
model/language, its default, and what it does.

**Why this priority**: The user explicitly asked for "clear and documented" config.
Undocumented env vars are as good as hardcoded — nobody changes what they can't find.

**Independent Test**: Cross-check that every new env var introduced by this feature
appears in the README Tunables table with a default and a one-line description, and that
the example config shows a non-DS, non-Florianópolis example.

**Acceptance Scenarios**:

1. **Given** the feature is complete, **When** a reader scans the README Tunables table,
   **Then** every new env var is listed with name, default, and description.
2. **Given** a fresh checkout with an empty `.env`, **When** the pipeline runs, **Then**
   it behaves exactly as it does today (defaults reproduce current behavior).

---

### Edge Cases

- **Empty / unset variable** → the documented default applies (current behavior), never a
  crash and never an empty-list that drops everything.
- **Malformed list value** (e.g. stray commas, blank items) → parsed leniently, blanks
  ignored, falls back to default if it yields nothing usable; a warning is logged.
- **Hub-city homonyms** (e.g. a bare "São José" that exists in several states) → the
  default must keep today's São Paulo-homonym rejection; a user-provided city list is
  matched as the user wrote it, with disambiguation left to the user's chosen city strings.
- **Empty seed but populated local file (or vice-versa)** → the effective blocklist is
  whichever source has entries; both empty → no hard title drops, and the pipeline still
  filters via the embedding scope gate + LLM (default-in-scope, Constitution Principle II).
- **Same term in both seed and local file** → deduplicated; it applies once, no error.
- **`keywords.example.yaml` drift** → the versioned example must stay structurally in
  sync (Constitution Principle V) and must not encode the current user's personal data.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The set of cities accepted for **hybrid** work MUST be configurable via a
  documented environment variable. Its default MUST reproduce today's Grande
  Florianópolis hub set (Florianópolis, Floripa, São José-SC, Palhoça, Biguaçu),
  including the current São Paulo-homonym rejection for the default set.
- **FR-002**: The policy deciding which **remote** job locations are accepted/rejected
  (today: a blocklist of foreign countries) MUST be configurable via a documented
  environment variable, with a default that reproduces today's rejected-country list.
- **FR-003**: The deterministic job-**title scope blocklist** (`out_of_scope_title`) MUST
  no longer be hardcoded to the DS/ML profile. Its patterns MUST be assembled at runtime
  from the **union, deduplicated**, of two sources: (a) a **seed** set defined in
  `config/keywords.yaml` as a structured list (human-readable reason → regex) that the user
  edits per profession, and (b) a **local file** that accumulates the user's own
  out-of-scope terms — the existing learn-from-marks blocklist (`learned_scope_blocklist`,
  Constitution Principle VII). Note: this "local file" is not a new file — it is the terms
  already accumulated in `vagas_historico.json` from the user's "Escopo incorreto" marks.
  The code reads both, dedupes, and applies the merged set.
  The default seed, versioned in `config/keywords.example.yaml`, MUST reproduce today's
  DS/ML drop/keep decisions for the current user.
- **FR-004**: The **language** in which the LLM writes strengths/gaps/verdict MUST be
  configurable via a documented environment variable, defaulting to Portuguese.
- **FR-005**: Every new configuration value MUST be read through the existing
  `src/settings.py` env helpers (Constitution Principle IV) and MUST have a sensible
  default, so the pipeline runs unchanged with an empty `.env`.
- **FR-006**: With an empty `.env`, the pipeline's observable behavior (which jobs are
  dropped/kept, in which region, in which language) MUST be byte-for-byte equivalent to
  the pre-feature behavior for the current DS/ML Florianópolis user. Existing filter tests
  (`tests/test_pipeline.py`, `tests/test_scope_filtering.py`) MUST stay green.
- **FR-007**: Every new environment variable MUST be documented in the README Tunables
  table (name, default, one-line description) and, where it has a value a user is expected
  to change per-profile, reflected in `config/keywords.example.yaml`.
- **FR-008**: No data specific to the current user (their region, their profession's
  off-track roles, their language) MAY remain hardcoded in source after this feature.
  Such data MUST live either in an env var default or in the example config, clearly
  labeled as a replaceable default.
- **FR-009**: Generalization MUST preserve the load-bearing constitutional policies:
  default-in-scope matching (II), the learn-from-marks blocklist (VII), and never-crash
  resilience (VI). A misconfigured or empty knob MUST never cause a good job to be silently
  dropped nor crash the run.

### Key Entities

- **Tunable knob**: a named environment variable governing one generalization axis
  (hybrid cities, remote region policy, title scope, output language); has a name, a
  documented default reproducing current behavior, and a README entry.
- **Per-profile config**: the values a user is expected to change to make the app theirs
  (search terms, profile, region, scope, language) — surfaced in the example config, kept
  out of git for real data (Constitution Principle V).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user targeting a **different profession** can, by changing only
  environment variables and their config file (no source edits), run the pipeline and have
  their target-role titles survive the scope gate.
- **SC-002**: A user targeting a **different location / work model** can, by env + config
  only, have hybrid jobs in their region pass and out-of-region jobs rejected.
- **SC-003**: With an empty `.env`, 100% of the existing filter test cases
  (`test_pipeline.py`, `test_scope_filtering.py`) pass unchanged — defaults reproduce
  current behavior.
- **SC-004**: A reader can find, in a single README table, every environment variable
  introduced by this feature, each with a default and a one-line description.
- **SC-005**: A code search for the current user's specifics (their region's city names,
  their profession's off-track role list, the forced output language) finds them only as
  documented defaults / example-config values, never as inline business logic.
- **SC-007**: The effective title blocklist equals the deduplicated union of the config/env
  seed and the local learned file; adding a term to either source makes it apply, and a
  duplicate across both applies exactly once.
- **SC-006**: Any single knob set to an empty or malformed value results in the documented
  default behavior and a logged warning — never a crash and never an all-dropped run.

## Assumptions

- **Remote region policy stays a blocklist by default.** The region is already restricted
  by LinkedIn's `geo_id`; the country blocklist is a secondary guard, so keeping it a
  (configurable) blocklist with today's list as default is the least-surprising choice.
  Country/geo_id lookup remains the user's responsibility (documented in the example).
- **Hub-city matching stays a normalized substring match.** Users list their own hub
  cities; the São José-SC homonym special-case is preserved only for the default set, and
  disambiguation for user-provided cities is delegated to how the user writes the city
  string (e.g. including the state). No new geocoding is introduced.
- **The title scope blocklist becomes seed + local, unioned.** The hardcoded DS/ML regex
  list moves into a `config/keywords.yaml` *seed* (structured reason → regex; default = today's
  set, versioned in `keywords.example.yaml`); the existing learn-from-marks store stays the
  *local file*. `out_of_scope_title` reads both, dedupes,
  and applies the union — so a user starts from a seed for their profession and refines it
  by marking "Escopo incorreto" in the web UI, with no code edits. This preserves
  Constitution Principle VII (learn-from-marks) and II (default-in-scope).
- **This feature is configuration generalization only.** It does not add new search
  sources, new scoring, or a settings UI — knobs are env vars + the existing YAML config,
  consistent with Constitution Principle IV. A web-based settings editor is out of scope.
- **Portuguese remains the default** for config keys (Principle V) and LLM output; the
  feature adds the ability to override output language, not a full i18n of the app or UI.
- **The example config gains a non-DS, non-Florianópolis illustration** (or comments) so
  the generalization is demonstrated, without changing the current user's real (git-ignored)
  `keywords.yaml`.

## Post-ship correction (2026-07-31)

**FR-006 was violated by this feature's own implementation and is fixed in feature
[004-workplace-model-accuracy](../004-workplace-model-accuracy/spec.md) (its FR-004).**

FR-006 requires that with an empty `.env` the observable behavior be equivalent to the
pre-feature behavior. Commit `1d6de95` changed `scraper.workplace_matches` from substring
matching (`if city in location_norm`) to exact comma-component matching, in order to reject
the São José-SP homonyms required by FR-001. That was the right intent, but the method also
rejected LinkedIn's **metro-area location strings**:

```
workplace_matches("Florianópolis, SC",      "hibrido") -> True
workplace_matches("Florianópolis e Região", "hibrido") -> False   # regression
workplace_matches("Grande Florianópolis",   "hibrido") -> False   # regression
```

`"<City> e Região"` is a real and common LinkedIn location (confirmed live: "Porto Alegre e
Região", "Belo Horizonte e Região", "São Paulo e Região"). The user's history holds **7**
hybrid records at `Florianópolis e Região`, all collected before 2026-07-24 under the old
substring logic; from that date they were silently dropped — a valid job in the target
region being discarded, the failure mode Constitution Principle II exists to prevent.

Root cause of the miss: FR-001's acceptance criteria named the homonyms to reject but never
enumerated the location *formats* LinkedIn actually emits, and no test covered a metro-area
string. Feature 004 strips the metro qualifier before the whole-component match, so both
requirements hold at once, and adds the regression test that was missing.

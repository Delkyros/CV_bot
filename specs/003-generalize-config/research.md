# Phase 0 Research: Generalize the pipeline

Six decisions. Each names what was chosen, why, and what was rejected. No open
NEEDS CLARIFICATION remain (the seed-location question was resolved in the spec's
Clarifications section).

---

## D1 — `env_list()` helper in `src/settings.py`

**Decision**: Add `env_list(name, default)` alongside the existing `env_str/float/int/bool`.
Parse a comma-separated string, strip each item, drop blanks; if the result is empty, return
`default` and log a warning (mirrors the other helpers' malformed-value behavior).

**Rationale**: Constitution Principle IV literally names `env_list` as part of the helper set,
but it was never implemented. Both list-valued knobs (hub cities, rejected countries) need it.
Comma-separated is the least-surprising `.env` convention and matches how a human types a list.

**Alternatives rejected**: JSON-in-env (ugly to type, over-engineered for a flat list);
per-item indexed vars `FOO_1/FOO_2` (no upside, harder to read).

---

## D2 — Hybrid hub cities → `SCRAPER_HYBRID_HUB_CITIES`

**Decision**: `scraper.workplace_matches` reads the hub-city list lazily from
`env_list("SCRAPER_HYBRID_HUB_CITIES", ["florianopolis", "floripa", "palhoca", "biguacu"])`
(via a small `_hybrid_hub_cities()` reader, matching the existing `_max_retries()` pattern).
The São José-SC homonym special-case (`dos campos` / `do rio preto` rejection, SC-marker
requirement) is kept **only for the default set** and applies when `"sao jose"` is among the
configured cities — it is a documented default behavior, not general geocoding.

**Rationale**: Substring match on normalized city names is exactly today's logic; only the
source of the list changes. Keeping the homonym guard tied to the default preserves the current
tests byte-for-byte. Users targeting other cities list unambiguous names (or include the state,
e.g. `"sao paulo"`), so they don't need the SC-specific guard.

**Alternatives rejected**: geoId/geocoding lookup per location (new dependency, network calls,
far beyond "make it configurable"); generalizing the homonym handling to all cities (speculative
— YAGNI; the user disambiguates via the string they write).

---

## D3 — Remote rejected countries → `SCRAPER_REMOTE_REJECTED_COUNTRIES`

**Decision**: Keep the remote gate as a **blocklist** of foreign-country tokens, read from
`env_list("SCRAPER_REMOTE_REJECTED_COUNTRIES", [<current 13 tokens>])`. A remote job whose
normalized location contains any token is rejected; otherwise kept.

**Rationale**: The region is already restricted by the search's `geo_id`; this list is a
secondary "leaked foreign posting" guard. Blocklist-with-current-default is the minimal change
and keeps `test_workplace_matches_remote_rejects_foreign` green. An allowlist would invert the
default-keep posture and risk dropping valid domestic postings whose text lacks the country name
(the very bug the current comment warns about).

**Alternatives rejected**: allowlist of accepted countries (inverts default-keep, Principle II
risk); coupling to `geo_id` only (loses the secondary guard that catches leaks).

---

## D4 — Title scope seed → `escopo_fora_de_alvo` in `keywords.yaml`

**Decision**: Move `_OUT_OF_SCOPE_TITLE_PATTERNS` out of `text_signals.py` into a
`escopo_fora_de_alvo` list in `config/keywords.yaml` — a list of `{motivo: <reason>, padrao: <regex>}`
entries. `main.py` loads it, and `out_of_scope_title(title, patterns)` receives the compiled
patterns instead of using a module constant. The current DS/ML set becomes the versioned default
in `config/keywords.example.yaml`. The effective title blocklist at runtime is the
**deduplicated union** of this seed and the learn-from-marks source (D5).

**Rationale**: Resolved in spec Clarifications — the patterns carry a human-readable reason and
are per-profile personal data, so YAML (Principle V) is their home, not flat `.env`. Injecting
patterns as a function argument keeps `text_signals` free of config/IO and keeps the function
unit-testable (tests pass an explicit pattern list).

**Alternatives rejected**: env var of `;`-joined regex (loses the reason labels, unreadable in
`.env`); keep the list in code as a "default" (violates FR-008 "no user-specific data in
source"); an on/off flag only (all-or-nothing; loses per-profile tailoring).

**Backward-compat note**: `out_of_scope_title` gains a required `patterns` argument. The current
callers are `main.py` (one call) and the tests. Tests will load the default seed from
`keywords.example.yaml` (or pass an inline list), so they stay meaningful and green.

---

## D5 — The "local file" union source = existing learn-from-marks blocklist

**Decision**: Reuse `main.learned_scope_blocklist(history)` (titles the user marked "Escopo
incorreto") as the *local* half of the union. Do **not** introduce a new standalone file.
`main.py` builds `seed_patterns ∪ learned_keys`, deduplicated, and both feed the deterministic
title drop in `analyze_and_filter_jobs` (as they already do — gate (a) regex seed, gate (b)
exact learned keys).

**Rationale**: `vagas_historico.json` already *is* the local file that accumulates the user's
out-of-scope terms (Principle VII). Adding a second file duplicates state and risks drift.
"Dedupe" means: (i) collapse duplicate seed entries, and (ii) skip a learned key already covered
by a seed pattern. The two matching mechanisms (regex vs exact-key) stay as-is; the union is at
the "sources of truth" level, matching the user's "olha env e arquivo local, deduplica" intent.

**Alternatives rejected**: a new `config/scope_blocklist.local.txt` (redundant with history,
new write path, Principle III surface); merging learned keys into the regex engine (needless
re-compilation, no behavior gain).

---

## D6 — LLM output language + genericized career-track examples

**Decision (language)**: `matcher._build_prompt` interpolates
`env_str("LLM_OUTPUT_LANGUAGE", "Portuguese")` into the "Write the strengths, gaps and verdict
in {language}" line. Default `Portuguese` → today's prompt.

**Decision (career-track examples)**: Replace the enumerated DS/ML off-track list in the
`core_role_compatible` instruction (prompt line ~118) with a profile-driven instruction: judge
compatibility against "the candidate's primary professional area **as described in the profile
above**", keeping the *generic* guidance (anchor on title/central function, not incidental
tech-stack overlap) but dropping the DS-specific enumeration.

**Rationale**: FR-008 requires no user-specific data hardcoded; the enumerated list ("mobile,
QA, BI, Qlik, Power BI, market research, analista de sistemas, Node/graduate…") is exactly the
current user's off-track set. The prompt already receives the full profile, so the model can
anchor on it. LLM output is non-deterministic, so FR-006's "byte-for-byte" requirement applies
to the **deterministic filters** (D2–D5), not to LLM prose — genericizing the examples does not
regress a testable decision.

**Alternatives rejected**: feed the `escopo_fora_de_alvo` reasons into the prompt (couples the
deterministic seed to prompt text — extra complexity, and the seed is title-regex not prose);
leave the DS list in the prompt (violates FR-008).

---

## Cross-cutting: never-crash & test posture

- Malformed `env_list` → default + warning (D1). Bad regex in `escopo_fora_de_alvo` → skip that
  entry with a warning, keep the rest (Principle VI); a single bad pattern never aborts the run.
- Existing deterministic-filter tests stay green because every default reproduces today's value.
  New cases added: a non-Floripa hybrid city passing (D2), and a seed∪learned dedupe case (D4/D5).
- Per the repo rule, tests are written but **not executed** here without the user's go-ahead.

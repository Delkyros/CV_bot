# Quickstart / Validation: Generalize the pipeline

Read-only validation that the five knobs work and that defaults preserve today's behavior.
Run from repo root with `.venv/Scripts/python.exe`. (Per repo policy, the maintainer runs the
test suite — these are the scenarios it must cover.)

## Prerequisites

- `config/keywords.yaml` present (copy from `keywords.example.yaml`).
- No feature env vars set (to prove the defaults path).

## Scenario 1 — Defaults reproduce current behavior (SC-003, FR-006)

Run the existing deterministic-filter tests unchanged:

```bash
.venv/Scripts/python.exe -m pytest tests/test_pipeline.py tests/test_scope_filtering.py -q
```

**Expected**: all green. Florianópolis/São José-SC/Palhoça/Biguaçu hybrid jobs pass; São Paulo
homonyms and other SC cities rejected; US remote jobs rejected; the current DS/ML off-track
titles still hard-dropped.

## Scenario 2 — Different location, work model (US2 / SC-002)

```bash
# .env
SCRAPER_HYBRID_HUB_CITIES=são paulo,guarulhos,osasco
```

**Expected**: a hybrid job in "São Paulo, SP" now passes `workplace_matches(..., "hibrido")`;
a hybrid job in "Florianópolis, SC" is now rejected. Verify via a unit test asserting both.

## Scenario 3 — Different remote country policy (US2 / SC-002)

```bash
SCRAPER_REMOTE_REJECTED_COUNTRIES=portugal,espanha
```

**Expected**: a remote job in "United States" now **passes** (no longer blocked), while one in
"Lisboa, Portugal" is rejected. Confirms the blocklist is fully env-driven.

## Scenario 4 — Different profession scope (US1 / SC-001)

Edit `config/keywords.yaml` `escopo_fora_de_alvo` for a non-DS role (or empty it):

```yaml
escopo_fora_de_alvo: []        # rely on learned blocklist + embedding + LLM only
```

**Expected**: a title today hard-dropped as off-track for DS (e.g. "Analista de Dados") is **no
longer** dropped by the title gate and reaches the downstream gates. With the default seed
restored, it is dropped again. Verify via `out_of_scope_title(title, patterns)` with both
pattern sets.

## Scenario 5 — Seed ∪ learned dedupe (SC-007)

Given a `escopo_fora_de_alvo` seed and a history with an "Escopo incorreto" mark on a title the
seed already matches:

**Expected**: the title is dropped exactly once (no error, no double log); a title only in the
learned set is still dropped; a title only in the seed is still dropped. Verify via a test that
builds the union and asserts membership + no duplicate.

## Scenario 6 — Output language (US3)

```bash
LLM_OUTPUT_LANGUAGE=English
```

**Expected**: `matcher._build_prompt(...)` contains "in English"; with the var unset it contains
"in Portuguese". The JSON response contract and `match_score` are unchanged. Verify by asserting
the substring in the built prompt (no LLM call needed).

## Scenario 7 — Robustness (FR-009, Principle VI)

```bash
SCRAPER_HYBRID_HUB_CITIES=,,   # malformed / empty
```

**Expected**: falls back to the default 4 cities with a logged warning; the run does not crash.
Likewise, an uncompilable `padrao` in `escopo_fora_de_alvo` is skipped with a warning while the
rest load.

## Scenario 8 — Documentation & no-inline-data (SC-004, SC-005, FR-007, FR-008)

- The README Tunables table lists `SCRAPER_HYBRID_HUB_CITIES`, `SCRAPER_REMOTE_REJECTED_COUNTRIES`,
  `LLM_OUTPUT_LANGUAGE`, and the `escopo_fora_de_alvo` config key, each with default + effect.
- `grep` for the moved literals (e.g. `biguacu`, `republica dominicana`, `analista de sistemas`)
  finds them only in `settings.py` defaults / `keywords.example.yaml`, not in business logic.

# Implementation Plan: Generalize the pipeline for any profile & any search

**Branch**: `003-generalize-config` | **Date**: 2026-07-24 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/003-generalize-config/spec.md`

## Summary

Remove the five hardcodes that lock the pipeline to one user (a DS/ML candidate in
Grande Florianópolis) and put each behind a documented, default-preserving knob:

1. **Hybrid hub cities** → `SCRAPER_HYBRID_HUB_CITIES` env list (default = the 4 current cities).
2. **Remote rejected countries** → `SCRAPER_REMOTE_REJECTED_COUNTRIES` env list (default = current list).
3. **Title scope seed** → `escopo_fora_de_alvo` key in `config/keywords.yaml` (reason → regex),
   default versioned in `keywords.example.yaml`; unioned+deduped with the existing
   learn-from-marks blocklist.
4. **LLM output language** → `LLM_OUTPUT_LANGUAGE` env (default `Portuguese`).
5. **LLM `core_role_compatible` career-track examples** (prompt line ~118) → genericized to
   reference "the candidate's primary area as described in the profile" instead of an
   enumerated DS/ML list (FR-008: no user-specific data hardcoded).

The enabling primitive is `env_list()` in `src/settings.py` — the last of the
`env_str/float/int/bool/list` helpers the constitution (Principle IV) already names but
which was never implemented. Everything else is plumbing: read the knob, default to
today's value, document it.

## Technical Context

**Language/Version**: Python 3.11 (`.venv/Scripts/python.exe`)

**Primary Dependencies**: PyYAML (config), existing `src/settings.py` env helpers, stdlib `re`.
No new dependency.

**Storage**: `config/keywords.yaml` (git-ignored per-user config) + `config/keywords.example.yaml`
(versioned template); `.env` for env vars; `vagas_historico.json` for the learn-from-marks
source (unchanged, read-only for this feature).

**Testing**: pytest (`tests/test_pipeline.py`, `tests/test_scope_filtering.py`).

**Target Platform**: Local Windows / Docker Linux — same as the existing pipeline.

**Project Type**: Single project (pipeline + Flask UI). No new components.

**Performance Goals**: No change — the knobs are read once per run (env) / once per call
(cheap). No hot-path impact.

**Constraints**: Empty `.env` MUST reproduce today's behavior exactly (FR-006); deterministic
filter tests MUST stay green (SC-003); never crash on malformed input (FR-009, Principle VI).

**Scale/Scope**: ~5 source files touched (`settings.py`, `scraper.py`, `text_signals.py`,
`matcher.py`, `main.py`), the example config, the README, and their tests. No architectural change.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Impact | Verdict |
|-----------|--------|---------|
| I. Default-CLT | Not touched (contract classification unchanged). | ✅ N/A |
| II. Default-In-Scope | A misconfigured/empty scope knob MUST NOT drop good jobs; empty seed → fewer hard-drops, never more (FR-009). | ✅ Preserved |
| III. Merge-Don't-Overwrite | No history-writing code changes; learn-from-marks is read-only here. | ✅ N/A |
| IV. Everything Env-Configurable | This feature *implements* the principle — adds the missing `env_list` and moves 5 hardcodes to env/config with defaults. | ✅ Advances |
| V. Portuguese Config, English Code | New config key `escopo_fora_de_alvo` is Portuguese (consistent); code/comments English; example stays structurally in sync. | ✅ Preserved |
| VI. Never-Crash | Malformed env list / bad regex in seed → logged warning + documented default, never a crash (FR-009). | ✅ Preserved |
| VII. Self-Adjusting Scope Filter | The learn-from-marks blocklist is reused unchanged as the "local" union source; deterministic behavior preserved. | ✅ Preserved |

**No violations.** Complexity Tracking below is empty.

## Project Structure

### Documentation (this feature)

```text
specs/003-generalize-config/
├── plan.md              # This file
├── research.md          # Phase 0 — the 6 decisions
├── data-model.md        # Phase 1 — the knobs & config shape
├── quickstart.md        # Phase 1 — validation scenarios
├── contracts/
│   └── config-contract.md   # env var + YAML key contract
└── checklists/
    └── requirements.md  # spec quality checklist (from /speckit-specify)
```

### Source Code (repository root)

```text
src/
├── settings.py        # ADD env_list() helper
├── scraper.py         # workplace_matches: read hub cities + rejected countries from env
├── text_signals.py    # out_of_scope_title(title, patterns): patterns injected, not hardcoded
└── matcher.py         # _build_prompt: {output_language} + genericized core_role examples

main.py                # load escopo_fora_de_alvo seed + union with learned blocklist; pass down
config/keywords.example.yaml   # ADD escopo_fora_de_alvo default seed (current DS/ML set) + non-DS example comment
README (Tunables table)        # document the 4 new env vars + the new config key

tests/
├── test_pipeline.py        # workplace_matches: default-preserving + a non-Floripa case
└── test_scope_filtering.py # out_of_scope_title with injected seed; union dedupe case
```

**Structure Decision**: Single project, no new modules. The change is localized to the 4
`src/` files that host the hardcodes plus `main.py` (the one place that already loads
`keywords.yaml` and builds the learned blocklist), consistent with the existing lazy-env-read
pattern (`scraper._max_retries()` etc.).

## Complexity Tracking

> No constitution violations — nothing to justify.

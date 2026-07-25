---

description: "Task list for 003-generalize-config"
---

# Tasks: Generalize the pipeline for any profile & any search

**Input**: Design documents from `specs/003-generalize-config/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/config-contract.md](./contracts/config-contract.md)

**Tests**: Included. This feature is filter/config logic guarded by `tests/test_pipeline.py` and
`tests/test_scope_filtering.py`; the constitution requires these stay green (SC-003) and a case
be added for any new drop/keep behavior. Per repo policy, tests are **written but not executed**
without the maintainer's go-ahead.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: US1..US4 from spec.md
- Exact file paths included

---

## Phase 1: Setup

No new project scaffolding — this feature edits existing modules only. Nothing to do here.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The one primitive both list-valued knobs (US2) depend on.

- [x] T001 Add `env_list(name, default)` to `src/settings.py`: split a comma-separated env value, `strip()` each item, drop blanks; return `default` when the result is empty; log a warning only when a non-empty raw value parsed to nothing (mirror the existing `env_int`/`env_float` malformed-value contract). Add a `demo()`/`__main__` assert self-check covering unset, blank, `"a, b ,,c"`, and malformed-only input.

**Checkpoint**: `env_list` importable and self-checked → US2 can proceed. (US1, US3, US4 do not depend on it.)

---

## Phase 3: User Story 1 — Search for a different profession (Priority: P1) 🎯 MVP

**Goal**: The title scope gate and the LLM prompt no longer assume a DS/ML candidate; the seed
lives in config, unioned with the learn-from-marks blocklist.

**Independent test**: With the default seed restored, current off-track titles still drop; with a
different/empty `escopo_fora_de_alvo`, a previously-dropped title (e.g. "Analista de Dados")
survives the title gate (spec US1 acceptance 1–3).

- [x] T002 [US1] Add the `escopo_fora_de_alvo` key to `config/keywords.example.yaml`: port every entry of the current `_OUT_OF_SCOPE_TITLE_PATTERNS` into a YAML list of `{motivo: <reason>, padrao: <regex>}` (this becomes the versioned default seed). Add a one-line comment that it is a replaceable per-profile default.
- [x] T003 [US1] Refactor `src/text_signals.py`: change `out_of_scope_title(title)` → `out_of_scope_title(title, patterns)` where `patterns` is a list of `(motivo, compiled_regex)`; remove the module-level `_OUT_OF_SCOPE_TITLE_PATTERNS` constant. Keep `normalize_text` matching. (Compilation/skip-bad-regex happens at load time in T004.)
- [x] T004 [US1] In `src/main.py`: load `escopo_fora_de_alvo` from the config dict, compile each `padrao` (skip an uncompilable entry with a logged warning, keep the rest — Principle VI), build the seed pattern list once per run, and pass it into the `out_of_scope_title(...)` call inside `analyze_and_filter_jobs`. Union is already achieved by the two existing gates (seed regex + `learned_scope_blocklist` exact keys); ensure a title matched by both is dropped once with no duplicate log (dedupe — SC-007).
- [x] T005 [US1] In `src/matcher.py` `_build_prompt`, replace the enumerated DS/ML "different career track" examples in the `core_role_compatible` instruction (line ~118) with profile-driven wording: judge against "the candidate's primary professional area as described in the profile above", keeping the generic guidance (anchor on the job title / central function, not incidental tech-stack overlap). No signature change. (FR-008.)
- [x] T006 [P] [US1] Update `tests/test_scope_filtering.py`: call `out_of_scope_title(title, patterns)` with the default seed loaded from `keywords.example.yaml`; keep the existing drop/keep cases green; add a case proving a non-default (or empty) seed lets a previously-dropped title through, a seed∪learned dedupe case (dropped exactly once), and a robustness case where the seed contains one uncompilable `padrao` — that entry is skipped (logged), the remaining valid patterns still load and drop, and no exception propagates (SC-006, Principle VI).

**Checkpoint**: US1 independently testable and complete.

---

## Phase 4: User Story 2 — Different location & work model (Priority: P1)

**Goal**: Hybrid hub cities and remote rejected countries come from env, defaults preserve today.

**Independent test**: default env → current SC/foreign behavior; a changed
`SCRAPER_HYBRID_HUB_CITIES` passes that city and rejects Florianópolis; a changed
`SCRAPER_REMOTE_REJECTED_COUNTRIES` flips which remote locations pass (spec US2 acceptance 1–4).

**Depends on**: T001 (`env_list`).

- [x] T007 [US2] In `src/scraper.py`, add `_hybrid_hub_cities()` = `env_list("SCRAPER_HYBRID_HUB_CITIES", ["florianopolis","floripa","palhoca","biguacu"])` and `_remote_rejected_countries()` = `env_list("SCRAPER_REMOTE_REJECTED_COUNTRIES", [<current 13 tokens>])` (lazy readers, matching `_max_retries()` style). Rewrite `workplace_matches` to iterate these lists instead of the inline literals; keep the São José-SC homonym guard active only when `"sao jose"` is among the configured cities (default set).
- [x] T008 [P] [US2] Update `tests/test_pipeline.py`: keep the existing default-preserving `workplace_matches` cases green; add a `monkeypatch.setenv("SCRAPER_HYBRID_HUB_CITIES", ...)` case (new city passes, Florianópolis rejected) and a `SCRAPER_REMOTE_REJECTED_COUNTRIES` case (US passes when removed from the list).

**Checkpoint**: US2 independently testable and complete.

---

## Phase 5: User Story 3 — Output language (Priority: P2)

**Goal**: The LLM writes strengths/gaps/verdict in a configurable language; default Portuguese.

**Independent test**: unset → prompt says "in Portuguese"; `LLM_OUTPUT_LANGUAGE=English` → "in English"; score/JSON contract unchanged (spec US3 acceptance 1–2).

**Depends on**: T005 (same function `_build_prompt` / same file `matcher.py`).

- [x] T009 [US3] In `src/matcher.py` `_build_prompt`, interpolate `env_str("LLM_OUTPUT_LANGUAGE", "Portuguese")` into the "Write the strengths, gaps and verdict in {language}" line. No signature change; falsy/unset → "Portuguese".
- [x] T010 [P] [US3] Add a test (extend `tests/test_fewshot_prompt.py` or a new `tests/test_prompt_language.py`) asserting the built prompt contains "in Portuguese" by default and "in English" when `LLM_OUTPUT_LANGUAGE=English` is set (no LLM call).

**Checkpoint**: US3 independently testable and complete.

---

## Phase 6: User Story 4 — Discover & document every knob (Priority: P2)

**Goal**: Every new knob is findable in one README table and illustrated in the example config.

**Independent test**: the README Tunables table lists all 4 knobs with default + effect; the
example config shows a non-DS/non-Floripa illustration (spec US4 acceptance 1–2).

**Depends on**: T002, T007, T009 (the knobs must exist to be documented accurately).

- [x] T011 [US4] Add rows to the README Tunables table for `SCRAPER_HYBRID_HUB_CITIES`, `SCRAPER_REMOTE_REJECTED_COUNTRIES`, `LLM_OUTPUT_LANGUAGE`, and the `escopo_fora_de_alvo` config key — each with name, default, and one-line effect (FR-007).
- [x] T012 [US4] In `config/keywords.example.yaml`, add commented illustrations for a non-DS profile and a non-Florianópolis city (e.g. `# SCRAPER_HYBRID_HUB_CITIES=são paulo,guarulhos` and a sample `escopo_fora_de_alvo` entry for another track), demonstrating the generalization without touching the real git-ignored `keywords.yaml`.

**Checkpoint**: US4 complete.

---

## Phase 7: Polish & Cross-Cutting

- [x] T013 Run the import smoke test (`.venv/Scripts/python.exe -c "import main; from src import scraper, matcher, text_signals, settings"`) and grep the `src/*.py` sources to confirm the moved literals (`biguacu`, `republica dominicana`, `analista de sistemas`, the DS-track enumeration) survive only as `settings.py` defaults or in `keywords.example.yaml`, never in business logic (SC-005, FR-008).

---

## Dependencies

```text
T001 (env_list) ──────────────► T007 (US2 scraper)
                                     └► T008 (US2 tests)

T002 (seed in example) ─┐
T003 (out_of_scope sig) ─┼──► T004 (main.py load+union) ──► T006 (US1 tests)
                         │
T005 (prompt genericize, matcher.py) ──► T009 (US3 language, same fn) ──► T010 (US3 test)

T002 + T007 + T009 ──► T011, T012 (US4 docs)

all impl ──► T013 (polish)
```

- **US1** and **US2** are independent of each other (different files) and can proceed in parallel
  once T001 is done for US2. US1 needs no foundational task.
- **US3 depends on US1's T005** only because both edit `matcher._build_prompt` — sequence them to
  avoid a merge conflict, not because of logic.
- **US4 (docs)** comes last so it documents the knobs as actually implemented.

## Parallel execution examples

- After T001: start T002/T003/T005 (US1, different files) and T007 (US2) concurrently.
- Test-writing tasks T006, T008, T010 are `[P]` — different test files, independent.
- T004 must wait for T002+T003 (it wires them together).

## Implementation strategy

- **MVP = US1** (profession scope): it removes the most damaging hardcode — the one that silently
  deletes the correct jobs for any non-DS user. Shippable and testable on its own.
- **Next = US2** (location/work model): the second P1; unblocks other regions.
- **Then US3, US4** (P2): language + documentation round out "any user, clearly documented".
- Each story is a complete increment; defaults keep every prior story's behavior intact.

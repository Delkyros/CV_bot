---

description: "Task list for 004-workplace-model-accuracy"
---

# Tasks: Work-model accuracy — mitigate LinkedIn's leaky remote filter

**Input**: Design documents from `specs/004-workplace-model-accuracy/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md)

**Tests**: Included. 13 tests added to `tests/test_pipeline.py`, then 21 redundant parametrized
cases removed in the ponytail pass (237 → 250 → **229**). Per repo policy tests are not
executed without the maintainer's go-ahead; here the maintainer explicitly asked for the proof
run, so they were run — all green.

**Status**: **All tasks complete.** This list was reconstructed from the shipped code after
the fact (the feature started as an analysis request), so it records the order the work
*actually* happened, including the two mid-flight corrections. It is a record, not a
forecast.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Could have run in parallel (different files, no dependency)
- **[Story]**: US1..US5 from spec.md
- Exact file paths and symbol names included

---

## Phase 0: Investigation (no code)

This phase produced the feature. It is listed because every later task's justification is a
number it produced, and because re-running it is the only way to check whether LinkedIn has
changed.

- [X] **T001** Quantify the reported symptoms against `data/vagas_historico.json`: 3,522
      records, 219 `Localidade/Modelo incorreto` (**100% `workplace_type="remoto"`**), 115
      duplicate `(title, company)` groups / 199 extra entries / 70 still UI-visible.
- [X] **T002** Probe the Guest search API for a per-job work-model field. Enumerated every
      element class across all cards → title, company, location, listdate, benefits badge
      only. **No work-model field.**
- [X] **T003** Probe the `jobPosting` guest endpoint across 15 jobs (5 each from
      `f_WT=1/2/3`) → criteria list is always exactly 4 labels. **No work model.** Bonus
      finding: `f_WT=1` and `f_WT=3` returned identical IDs.
- [X] **T004** Probe the full public job page, 6 jobs (~300 KB each) → no tag element, no
      `workplaceType`/`workRemoteAllowed`/`jobLocationType` JSON, no model word in
      `<title>`/`meta`, no JSON-LD. ⇒ the tag is **login-only** (spec FR-013).
- [X] **T005** Establish the endpoint is deterministic per URL (same URL ×4 → identical IDs,
      same order). **This control is what makes T006 interpretable.**
- [X] **T006** Paired test `f_WT=2` vs no filter, 24 samples → identical result set in
      **3/24**. `f_WT=99` → 0 results, so the parameter is parsed. ⇒ leak is server-side.
- [X] **T007** Read the **official** LinkedIn Job Posting API docs → `workplaceTypes` enum
      and the location-format rule (Remote = Country / City+Country / Country Cluster);
      `alternateLocations` max 7 explains the multi-city copies. Confirmed no official doc
      exists for the guest endpoints.
- [X] **T008** Verify `distance` semantics: honoured on a city geoId (0→none, 25/50→hubs,
      100→Blumenau); **no-op on a country geoId** (control 3/6 vs test 3/6).
- [X] **T009** Measure candidate signals on the history: location shape 1.4/6.0/6.8/**13.5%**;
      title city/UF 4.9% recall; company-learned ceiling (80% of offenders offend once);
      **49 of 203 unreachable**.

---

## Phase 1: Setup

No scaffolding — this feature edits existing modules only. Nothing to do.

---

## Phase 2: Foundational (blocking prerequisite)

- [X] **T010** [US1] Rename `description_conflicts_with_remote` → **`conflicts_with_remote`**
      in `src/scraper.py` and reduce it to a delegation. Blocking: US1 and US5 both build on
      it, and the rename is what prevents a second, drifting title-specific predicate.
      Updated its two existing callers/tests.

---

## Phase 3: User Story 1 — Title gate (P1) 🎯 MVP

**Goal**: stop collecting remote-search hits whose title declares hybrid/on-site.
**Independent test**: a remote search over such cards collects none and fetches no description.

- [X] **T011** [US1] Call `conflicts_with_remote(title)` in `scrape_linkedin_jobs`
      (`src/scraper.py`), gated on `normalize_text(workplace_type) == "remoto"`, placed
      **after** `workplace_matches` and **before** `fetch_job_description` so a drop costs no
      request. (FR-001, FR-002, FR-003)
- [X] **T012** [US1] Test `test_title_gate_catches_mislabeled_remote_jobs` — titles **verbatim
      from the history**, all flagged by the user, one per distinct decision path. Parametrized
      so each failure names the real ad. (SC-001)
- [X] **T013** [US1] Test `test_title_gate_keeps_genuinely_remote_jobs` — control set: a real
      remote ad (no model signal at all), `Site Reliability Engineer` (the `on-site` pattern
      must not match `Site`) and `Remoto ou Híbrido` (the veto path). (SC-001)
- [X] **T014** [US1] Test `test_remote_search_drops_hybrid_titled_cards_before_downloading` —
      drives `scrape_linkedin_jobs` with fake cards in the real Guest-API shape and asserts
      `fetched == ["333"]`. **This is the one that matters**: the helper already existed and
      was correct; the bug was that nothing called it with the title.
- [X] **T015** [US1] Helper `_search_card(job_id, title, company, location)` in the test file,
      modelled on HTML captured live in T002.

**Checkpoint**: 16/219 historical errors caught, 0 false positives on the control.
Honest cost recorded in spec SC-002: also fires on 19 unflagged records, of which **2 the
user engaged with**.

---

## Phase 4: User Story 2 — Metro-area fix (P1)

**Goal**: stop discarding valid hybrid jobs in the target metro area.
**Independent test**: hub gate accepts the metro strings, still rejects the homonyms.

- [X] **T016** [US2] Add `_METRO_AREA_QUALIFIER` to `src/scraper.py`:
      `^(?:grande|regiao metropolitana de)\s+|\s+e\s+regiao$`.
- [X] **T017** [US2] Apply it inside `workplace_matches` — strip per comma-component **before**
      the whole-component hub match, so feature 003's homonym rejection is untouched. (FR-004)
- [X] **T018** [US2] Test `test_workplace_matches_hybrid_accepts_metro_area_strings` — asserts
      both directions: accepts `Florianópolis e Região` / `Grande Florianópolis` /
      `Região Metropolitana de Florianópolis`; still rejects
      `São José dos Campos e Região` / `Grande São Paulo` / `Joinville e Região`. (SC-003)
- [X] **T019** [US2] Record the regression in
      `specs/003-generalize-config/spec.md` as a **Post-ship correction**, naming the violated
      requirement (003's FR-006), the commit (`1d6de95`), and the process gap: FR-001 listed
      the homonyms to reject but never enumerated the location formats LinkedIn emits, and no
      test covered a metro string.

**Checkpoint**: 7 historical `Florianópolis e Região` records accepted again.

---

## Phase 5: `distance` passthrough (supports US2)

- [X] **T020** Add `distance=None` parameter to `scrape_linkedin_jobs` and append
      `&distance={distance}` only when `is not None`, so `0` is sent as a real radius and
      absence leaves LinkedIn's default. (FR-005)
- [X] **T021** Read `distancia` from each `filtros_busca` entry in `main.py:main()` and pass
      it through.
- [X] **T022** [P] Document `distancia` in `config/keywords.example.yaml` with the measured
      radius behaviour; set `distancia: 25` on the four hybrid filters in `config/keywords.yaml`.
- [X] **T023** Test `test_search_url_carries_distance_only_when_configured` — asserts
      `&distance=50` present, `distance` absent when unset, and `&distance=0` sent for `0`.
- [X] **T024** **Correction applied mid-flight**: an early probe suggested "omitting behaves
      like 25"; a later probe contradicted it (unset returned 10 cards, matching 50). The
      docstring and both YAML comments were rewritten to state the implicit default is **not
      stable** instead of asserting a value. (SC-006)

**Checkpoint**: `distance` wired and documented — and measured to have **zero** effect on the
219 errors, since all of them come from the country-geoId remote search. Reported as such
rather than presented as a win.

---

## Phase 6: User Story 3 — Location-shape risk label (P2)

**Goal**: show how much to trust each "remote" job, without hiding anything.

- [X] **T025** [US3] Add `remote_location_shape(job_location, search_location)` to
      `src/scraper.py` → `country` | `city_country` | `metro` | `city_state`. Country taken as
      the **last comma-component of the searched location**, so no country table is needed.
      (FR-006)
- [X] **T026** [US3] Document the measured error rate per bucket in the block comment above it,
      with sample sizes, so the numbers shipped in the UI have a provenance.
- [X] **T027** [US3] Populate `location_shape` on each kept job in `scrape_linkedin_jobs`
      (only when the search was remote) and persist it in `main.save_job_history`. (FR-011)
- [X] **T028** [US3] Expose `location_shape` in the `/api/jobs` payload (`webapp.py`).
- [X] **T029** [US3] Add the **Local·risco** column to the Relatório tab (`web/index.html`):
      `SHAPE_LABEL` (Portuguese label + measured rate in the tooltip), `fmtShape`, CSS, and
      `colspan` 4 → 5 on the detail row. (FR-007)
- [X] **T030** [US3] Add `SHAPE_RANK` so sorting orders by **measured risk** — alphabetical
      order on the enum would be `city_country < city_state < country < metro`, i.e. noise.
- [X] **T031** [US3] Tests `test_remote_location_shape` (7 real location strings) and
      `test_remote_location_shape_uses_the_configured_country` (Portugal + a city-level search
      location + `None` input). (SC-004)

**Checkpoint**: 1,236 jobs labelled high-risk, 167 of them genuinely wrong. **Nothing dropped.**

---

## Phase 7: User Story 4 — Learned location blocklist (P2)

**Goal**: skip repeat-offender companies without losing good employers.

- [X] **T032** [US4] Add `LOCATION_ERROR_CLASS`, `ENGAGED_STATUSES`,
      `DEFAULT_LOCATION_BLOCKLIST_MIN_ERRORS = 2` and `location_blocklist_min_errors()` to
      `main.py`; import `collections`. (FR-010)
- [X] **T033** [US4] Add `learned_location_blocklist(history)` — companies with ≥ threshold
      location marks **and** no `viewed`/`applied` job. The engagement veto is unconditional.
      (FR-009)
- [X] **T034** [US4] Add `excluded_companies` parameter to `scrape_linkedin_jobs`; skip at the
      card level, before the description download.
- [X] **T035** [US4] Build the blocklist once per run in `main()`, log its size, pass it to
      every search.
- [X] **T036** [US4] Tests `test_learned_location_blocklist_needs_repeats_and_no_engagement`
      (repeat-offender blocked; engaged company spared; single error spared; other error class
      ignored) and `test_learned_location_blocklist_threshold_is_env_tunable`. (SC-005)
- [X] **T037** [US4] README Tunables row for `LOCATION_BLOCKLIST_MIN_ERRORS`, including the
      measured warning against `1` (80% of offenders offend once ⇒ ~112 companies of blast
      radius for unpredictable errors).

**Checkpoint**: 16 companies blocked, 30/203 residual errors caught, **0 collateral**.

---

## Phase 8: User Story 5 — Make the next round measurable (P2)

**Goal**: stop being blind about the description guard.

- [X] **T038** [US5] Add `remote_conflict_evidence(text)` → `(onsite_hits, remote_hits)` to
      `src/scraper.py`, and make `conflicts_with_remote` delegate to it, so the two callers
      cannot diverge. (FR-008)
- [X] **T039** [US5] Record `workplace_evidence` (4 counts: title/desc × onsite/remote) on each
      kept job and persist it in `save_job_history`. Counts, not text — small history, no
      third-party prose stored.
- [X] **T040** [US5] Test `test_remote_conflict_evidence_separates_silence_from_veto` — asserts
      the two `False` cases are now distinguishable.
- [X] **T041** [US5] Test `test_scrape_skips_blocklisted_companies_and_records_model_evidence` —
      covers T034 **and** asserts a kept job records `desc_onsite > 0 and desc_remote > 0`,
      the vetoed case, with the blocklisted company skipped before download. (SC-007)

**Checkpoint**: every remote-search job now carries the features the next iteration needs.

---

## Phase 9: Validation & documentation

- [X] **T042** Full suite: **250 passed** (was 237). Existing filter tests green, satisfying
      feature 003's FR-006. Later trimmed to **229** by T046. (SC-008)
- [X] **T043** Validate the shipped code against the **real** history, not fixtures:
      `learned_location_blocklist` → 16 companies, 30/203, 0 collateral;
      `remote_location_shape` → 1.4% / 6.0% / 6.8% / 13.5% at n = 1645 / 319 / 148 / 1236.
      Both reproduce the Phase-0 measurements exactly.
- [X] **T044** [P] `node --check` on the extracted `<script>` block (JS syntax) and
      `yaml.safe_load` on both config files; `import main, webapp` smoke test.
- [X] **T045** [P] Write `spec.md`, `research.md`, `data-model.md`, then this `plan.md` and
      `tasks.md` from the shipped code.

---

## Phase 10: Ponytail pass (post-PR)

Requested after PR #14 opened ("limpar e melhorar… to achando q tem coisa demais nos testes").
Behaviour-preserving throughout: re-validated against the real history afterwards, with
16/219 and 30/203/0-collateral unchanged.

- [X] **T046** Cut the title fixtures from **28 parametrized cases to 7**. Justified by
      measurement, not taste: instrumenting `remote_conflict_evidence` over the fixtures showed
      the 16 "mislabeled" cases exercise **3** distinct pattern combinations (14 of them are the
      identical assertion) and the 12 "genuine remote" cases exercise **2** — 11 of which return
      `False` for the trivial reason that no on-site pattern matched at all, testing nothing
      about the gate. Kept one case per path plus the `Site Reliability Engineer` over-match
      guard. The offender list stays in `spec.md`/`research.md`: provenance is data, and data
      belongs in the spec, not archived as duplicate assertions. **237 → 250 → 229.**
- [X] **T047** Hoist `is_remote_search` out of the card loop in `scrape_linkedin_jobs`.
      `normalize_text(workplace_type) == "remoto"` was being recomputed **three times per card**
      from a value that is a function parameter and never changes.
- [X] **T048** Single-source the four measured rates in `web/index.html`: `SHAPE_RANK` restated
      the same numbers as `SHAPE_LABEL` 30 lines apart, so a re-measurement would have drifted
      them. `SHAPE_LABEL[s].rate` now drives both the tooltip and the sort. Also moved the
      definition above its first use instead of relying on TDZ.
- [X] **T049** Label the UI rates as a **snapshot**. Re-validating after the cleanup showed they
      had already moved (13.5 → 13.2%, 6.8 → 6.6%) because the scheduler had scraped more jobs.
      Rounded to 1 significant figure and tagged "jul/2026" — the *level* is what should be read,
      and pretending to a decimal that silently rots is worse than admitting the range.
- [X] **T050** Trim comments that restated `research.md`: `conflicts_with_remote`'s docstring
      (18 lines for a 2-line function), the rate table above `remote_location_shape` (a 4th copy
      of the same numbers), and the two overlapping work-model gate comments. Each now states
      what a reader of *that code* needs and points to the spec for the evidence.
- [X] **T051** Update the stale figures this pass created in `spec.md` (SC-008), `plan.md` and
      `tasks.md` — the docs written in T045 claimed 250 tests and a 12-title control set.

**Not done deliberately**: `test_conflicts_with_remote_is_conservative` (pre-existing) now
overlaps the new title tests on 3 of its 4 asserts, but its empty-string case is unique and it
predates this feature — deleting someone else's coverage was out of scope for a cleanup pass.
`city_country` and `metro` were left as separate enum values despite both rendering "médio":
merging is irreversible and destroys a distinction the meta-model may want.

---

## Dependencies & Execution Order

```
Phase 0 (investigation) ─── gates everything; every task below cites one of its numbers
        │
        └─> T010 (rename)  ─── blocks US1 and US5
                │
                ├─> Phase 3  US1  title gate          (independent)
                ├─> Phase 8  US5  evidence            (shares the helper with US1)
                │
Phase 4 US2 metro fix   ────── independent of US1 (different function)
Phase 5 distance        ────── independent (URL building only)
Phase 6 US3 shape label ────── independent; touches webapp.py + web/index.html
Phase 7 US4 blocklist   ────── independent; touches main.py only
        │
        └─> Phase 9 validation ─── after all of the above
```

**Truly independent** (different files/functions, could have been parallel): US2, US3, US4
and the `distance` work. Only US1 and US5 share `src/scraper.py`'s conflict helper, and only
through T010.

**Actual order shipped**: Phase 0 → T010 → US1 → US2 (both in one change, since both touch
`workplace_matches`'s neighbourhood and US2 was an active regression) → `distance` → US5 →
US3 → US4 → validation → docs. Driven by confidence: each change waited for the measurement
that justified it.

## Implementation Strategy

**MVP was US1 alone** — the title gate is 16 of 219 errors for one call site and zero
requests. Everything after it has strictly worse economics, which is why the phases are
ordered by measured confidence rather than by effort.

**US2 jumped the queue** on discovery: it was losing *good* jobs, which the constitution
treats as worse than showing bad ones, and it would never have surfaced from the user's
marks (you cannot flag a job you were never shown).

**The last three stories were shipped together** after the maintainer said "faz o que dá" —
each is small, none is a filter except the engagement-vetoed blocklist, and US5 exists purely
to make the next round measurable.

## Notes

- The residual is **49 of 203** errors, provably invisible to an anonymous client. Accepted by
  the maintainer as manual work (spec SC-009), not a defect to fix later.
- Two proposals were **rejected by the maintainer** during this feature and should not be
  resurrected without new evidence: history-wide repost dedupe (LinkedIn's
  `alternateLocations` makes the multi-city copies one posting) and authenticated tag
  scraping (ToS + account risk).
- `.specify/feature.json` still points at `003-generalize-config`; repointing it would change
  which feature the `/speckit-*` commands act on, so it was left alone deliberately.

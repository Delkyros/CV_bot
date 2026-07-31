# Feature Specification: Work-model accuracy — mitigate LinkedIn's leaky remote filter

**Feature Branch**: `004-workplace-model-accuracy`

**Created**: 2026-07-31

**Status**: Implemented (shipped before the spec was written — see *Process note*)

**Input**: User description: "dá uma olhada e faz uma análise das vagas com erros de local e escopo, pois estou sentindo que temos vagas duplicadas aparecendo e que não estamos levando em consideração os campos de hibrido e presencial. Olhe a documentação da api publica do linkedin para entender melhor como podemos filtrar para não errar tanto a localidade." → later: "a ideia é mitigar ao máximo… os 49 casos eu me viro manualmente"

## Context

The user triages every job in the web UI and marks the bad ones. Over 3,522 history
records they had flagged **219 jobs `Localidade/Modelo incorreto`** — jobs the pipeline
presented as remote that were actually hybrid or on-site. That is 6.2% of the history and
the single largest error class after scope.

Measured root cause: **LinkedIn's `f_WT=2` (remote) search filter is not reliably
applied.** In 24 paired samples (identical URL, with and without `f_WT=2`), 3 returned
*exactly* the unfiltered result set. A control established the endpoint is otherwise
deterministic per URL (same URL fetched 4× → identical IDs, same order), so those
collisions are the filter being dropped, not noise. All 219 flagged records carry
`workplace_type="remoto"`; **zero** came from the hybrid searches. The leak is entirely
on the remote side.

The compounding problem is that **no per-job work-model field is reachable without
authentication.** This was verified exhaustively (see `research.md`), because the obvious
fix — read the model from the job page — does not exist for a logged-out client:

- the search card exposes only title, company, location, post date, benefits badge;
- the `jobPosting` guest endpoint's criteria list is always exactly four labels
  (Nível de experiência, Tipo de emprego, Função, Setores) across 15 jobs sampled;
- the full public job page (6 jobs, ~300 KB each) contains no work-model tag element, no
  `workplaceType`/`workRemoteAllowed`/`jobLocationType` JSON, and no model word in
  `<title>` or `meta`;
- in one sample `f_WT=1` (on-site) and `f_WT=3` (hybrid) returned **identical** IDs — the
  guest surface does not even model the distinction.

The work-model tag the user sees in their browser is rendered by the authenticated UI.
Reading it would require a logged-in session (`li_at` cookie), which violates LinkedIn's
ToS and risks the account they are job-hunting with. **Out of scope for this feature.**

Therefore this feature does not *fix* the leak — it cannot. It mitigates what is
mitigable, using only signals available to an anonymous client, and is explicit about the
residual the user accepted to handle by hand.

### Process note

This feature was implemented before this spec existed: it began as an analysis request
and turned into changes as each finding was confirmed. The spec is written after the fact
and records measurements taken from the real history and live API probes, not intentions.
`plan.md` and `tasks.md` were deliberately **not** back-filled — writing them now would
be fiction. `research.md` and `data-model.md` are real artifacts and are included.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Stop being shown ads that announce their own work model (Priority: P1)

The user searches remote roles. LinkedIn leaks an ad titled
`Engenheiro de IA Pleno | Híbrido| São Paulo/SP`. Today it lands in the triage queue and
they mark it wrong by hand, having already paid a description download and an LLM match
call for it.

**Why this priority**: highest confidence, zero cost, and it fires before any network
request. The title is a cleaner signal than the description, whose stray "home office" in
a benefits list vetoes the existing description guard.

**Independent Test**: run a remote search over cards whose titles declare hybrid/on-site
and assert none is collected and no description is fetched for them.

**Acceptance Scenarios**:

1. **Given** a remote search, **When** a card's title matches a hybrid/on-site pattern and
   contains no remote wording, **Then** the job is skipped before its description is
   downloaded.
2. **Given** a title naming both models (`Data Scientist Senior - Remoto ou Híbrido`),
   **When** the gate runs, **Then** the job is kept — a remote possibility vetoes the drop.
3. **Given** a title like `Site Reliability Engineer`, **When** the gate runs, **Then** the
   job is kept (`Site` must not satisfy the on-site pattern).
4. **Given** a hybrid or on-site search, **When** the gate would apply, **Then** it does
   not run at all — the declaration agrees with the search.

---

### User Story 2 — Stop silently dropping valid jobs in the target metro area (Priority: P1)

The user's region is Greater Florianópolis. LinkedIn labels metro areas
`"<City> e Região"`. Since feature 003 those jobs were being **discarded**.

**Why this priority**: this is a live regression losing *good* jobs — the exact failure the
project's constitution exists to prevent — and it is the inverse of the bug the user
reported, so it would never have surfaced from their marks.

**Independent Test**: assert the hub gate accepts the metro strings while still rejecting
the homonyms feature 003 was written to reject.

**Acceptance Scenarios**:

1. **Given** a hybrid job at `Florianópolis e Região`, **When** the hub gate runs, **Then**
   it is accepted.
2. **Given** `Grande Florianópolis` or `Região Metropolitana de Florianópolis`, **When** the
   gate runs, **Then** they are accepted.
3. **Given** `São José dos Campos e Região` or `Grande São Paulo`, **When** the gate runs,
   **Then** they are rejected — the metro qualifier MUST NOT become a backdoor.

---

### User Story 3 — See how much to trust a "remote" job at a glance (Priority: P2)

The user opens the Relatório tab and wants to know which "remote" jobs are likely not
remote, without anything being hidden from them.

**Why this priority**: the strongest available signal (75% recall) but only 12% precision —
useful to the eye, unusable as a filter. Delivering it as a *filter* would destroy value;
delivering it as a *column* is pure gain.

**Independent Test**: classify real history locations and check the label matches the
measured error rate bucket.

**Acceptance Scenarios**:

1. **Given** a remote job at `São Paulo, SP`, **When** the report renders, **Then** it shows
   risk `alto` (measured 13.5% wrong).
2. **Given** a remote job at `Brasil`, **When** the report renders, **Then** it shows risk
   `baixo` (measured 1.4% wrong).
3. **Given** any risk level, **When** the pipeline runs, **Then** **no** job is dropped
   because of it.
4. **Given** the user sorts by the column, **When** it sorts, **Then** it orders by measured
   risk, not alphabetically by internal value.

---

### User Story 4 — Learn from repeat offenders without losing good employers (Priority: P2)

Some companies habitually advertise hybrid roles as remote. If the user has never engaged
with such a company, its ads are noise.

**Why this priority**: real but small (15% of the residual). Capped by data, not effort:
80% of offending companies offend exactly once, so nothing can predict a first offence.

**Independent Test**: build the blocklist from a synthetic history and assert only the
repeat-offender-never-engaged company is blocked.

**Acceptance Scenarios**:

1. **Given** a company with ≥2 `Localidade/Modelo incorreto` marks and no `viewed`/`applied`
   job, **When** collection runs, **Then** its cards are skipped before download.
2. **Given** a company with ≥2 such marks **but** one `applied` job, **When** the blocklist
   is built, **Then** it is **not** blocked — engagement always wins.
3. **Given** a company with exactly one mark, **When** the blocklist is built, **Then** it is
   not blocked.
4. **Given** marks of a *different* error class, **When** the blocklist is built, **Then**
   they are ignored.

---

### User Story 5 — Make the next iteration measurable (Priority: P2)

The largest untouched lever is the description guard, and its effectiveness **could not be
measured at all**: the history persists no description, and `conflicts_with_remote`
collapsed "said nothing about the model" and "said on-site but was vetoed by a remote
word" into the same `False`.

**Why this priority**: without it every further tuning is blind. It ships no user-visible
behavior, which is exactly why it would never get prioritized on its own.

**Independent Test**: assert the evidence helper distinguishes silence from veto, and that
a kept job records the veto.

**Acceptance Scenarios**:

1. **Given** text with no work-model wording, **When** evidence is extracted, **Then** both
   hit lists are empty.
2. **Given** text with on-site wording *and* a remote word, **When** evidence is extracted,
   **Then** both lists are non-empty while `conflicts_with_remote` is still `False`.
3. **Given** a job kept by a remote search, **When** it is persisted, **Then** the record
   carries the per-job on-site/remote hit counts for title and description.

---

### Edge Cases

- Location text absent or single-segment with no country match → classified `city_state`
  (the pessimistic bucket); it is a ranking label only, so nothing is lost.
- A search location that is a city (the hybrid filters pass
  `"São José, Santa Catarina, Brasil"`) → the country is still the last component, so shape
  classification stays correct.
- `distance=0` is a radius LinkedIn honours (it returns nothing), so it MUST be
  distinguishable from "unset".
- A company on the location blocklist that the user later marks `viewed`/`applied` → it
  leaves the blocklist on the next run, because the list is rebuilt from history each run.
- All work-model gates MUST be no-ops when `workplace_type` is unset.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: In a **remote** search, a job whose **title** explicitly declares a
  hybrid/on-site model and gives no remote signal MUST be skipped **before** its
  description is downloaded.
- **FR-002**: The remote-conflict decision MUST remain vetoed by any remote wording in the
  same text (Constitution Principle II — a job is dropped only on unambiguous evidence).
- **FR-003**: The single helper implementing FR-001/FR-002 MUST serve both the title and the
  description, so the two callers cannot drift apart.
- **FR-004**: The hybrid hub-city gate MUST accept LinkedIn's metro-area location strings
  (`"<hub> e Região"`, `"Grande <hub>"`, `"Região Metropolitana de <hub>"`) while continuing
  to reject same-prefix homonyms and non-hub cities. This corrects a feature-003 regression
  against its own **FR-006**.
- **FR-005**: LinkedIn's `distance` (search radius in miles around `geo_id`) MUST be
  configurable per search filter in `config/keywords.yaml`, and MUST be omitted from the URL
  when not configured so LinkedIn's default applies. `0` MUST be sent as a real value.
- **FR-006**: Each job kept by a remote search MUST persist a **location shape**
  classification of its location against the searched country. It MUST be derived without a
  country table (the country is the last component of the configured search location) so it
  generalizes to any region (Constitution Principle IV / feature 003).
- **FR-007**: The location shape MUST be surfaced in the web UI Relatório tab as a risk
  label with its measured error rate, sortable by **risk order**, and MUST NOT filter,
  hide, or drop any job.
- **FR-008**: Each job kept by a remote search MUST persist the per-job work-model
  **evidence** — the count of on-site and remote pattern hits for the title and for the
  description — so the vetoed case is distinguishable in later analysis.
- **FR-009**: A company MUST be skipped on sight when it has at least
  `LOCATION_BLOCKLIST_MIN_ERRORS` (default `2`) `Localidade/Modelo incorreto` marks **and**
  no job the user ever marked `viewed` or `applied`. The engagement veto is not optional.
- **FR-010**: The threshold in FR-009 MUST be env-configurable with its default documented
  in the README Tunables table, including the measured warning against setting it to `1`.
- **FR-011**: The new persisted fields MUST flow through `save_job_history` without
  disturbing the merge-don't-overwrite invariant on user-owned fields (Constitution
  Principle III).
- **FR-012**: No new gate may drop a job on a *missing* signal — every gate added here fires
  only on explicit positive evidence (Constitution Principle II).
- **FR-013**: Reading the work-model tag from an authenticated LinkedIn session is **out of
  scope**; the residual it would address MUST be documented rather than silently accepted.

### Key Entities

- **Location shape**: one of `country` | `city_country` | `metro` | `city_state`, derived
  from a remote job's location vs the searched country. Carries a measured error rate; a
  ranking signal, never a gate.
- **Work-model evidence**: four counts per job (`title_onsite`, `title_remote`,
  `desc_onsite`, `desc_remote`). `desc_onsite > 0 and desc_remote > 0` is the *vetoed* case
  — the leading suspect for a job the user will later flag.
- **Learned location blocklist**: normalized company names derived from the user's own
  marks each run, gated by the engagement veto. Sibling of the learned scope blocklist
  (Constitution Principle VII).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The title gate catches **16 of the 219** historical location errors, with
  **0** false positives on a control set of genuinely remote ads.
- **SC-002**: The title gate additionally fires on 19 records the user did not flag; 17 were
  already `irrelevant`/out-of-scope, and **2 were engaged with** (1 applied, 1 viewed). This
  cost is accepted and recorded, not hidden.
- **SC-003**: The metro-area fix restores acceptance of the **7** historical
  `Florianópolis e Região` records, while `São José dos Campos e Região`, `Grande São Paulo`
  and `Joinville e Região` stay rejected.
- **SC-004**: The location shape reproduces these error rates over the 3,348 remote records:
  `country` 1.4% (n=1645), `metro` 6.0% (n=319), `city_country` 6.8% (n=148), `city_state`
  13.5% (n=1236) — a ~10× spread between best and worst.
- **SC-005**: The learned location blocklist yields **16 companies**, catching **30 of the
  203** residual errors with **0** collateral on jobs the user viewed or applied to.
- **SC-006**: `distance` is verified to affect only city-level geoIds. On a country geoId it
  is a no-op: adding it changed the result set exactly as often as re-fetching the same URL
  (3/6 vs 3/6).
- **SC-007**: Every remote-search job persists a non-null `location_shape` and a populated
  `workplace_evidence`, making the description guard measurable on the next triage round.
- **SC-008**: The full suite passes (**229 tests**), including the pre-existing filter tests
  feature 003's FR-006 requires to stay green. The suite peaked at 250 and was trimmed to 229
  once the title fixtures were *measured* to cover only 5 distinct decision paths across 28
  cases — fewer tests, identical coverage, and the behaviour re-validated against the real
  history afterwards (16/219 and 30/203 unchanged).
- **SC-009**: **49 of the 203** residual errors are provably unreachable from the anonymous
  surface (location `Brasil`, silent title, no tag). Documented as the accepted manual
  residual, not a defect.

## Assumptions

- The user accepts handling the ~49 invisible cases manually (stated explicitly).
- Authenticated scraping stays off the table; if that changes, the work-model tag becomes
  directly readable and most of this feature becomes redundant.
- The `f_WT` leak rate (~3/24 samples) is LinkedIn-side and may change without notice; the
  mitigations degrade gracefully if it stops or worsens.
- Error rates per location shape were measured on one user's 2026 history in Brazil. The
  *mechanism* (LinkedIn's documented location-format rule) generalizes; the *percentages*
  shown in the UI do not necessarily.
- LinkedIn's guest endpoints are undocumented and may change; `research.md` records how each
  finding was verified so it can be re-checked rather than re-derived.

# Research: what LinkedIn actually exposes about a job's work model

**Feature**: 004-workplace-model-accuracy
**Date**: 2026-07-31

Everything below was verified against the live API or the user's real history. Each finding
records **how** it was checked so it can be re-verified cheaply — these are undocumented
endpoints and they drift. Where a claim could not be established, that is stated instead of
guessed.

## 1. Is there an official API?

**No usable one.** Verified against LinkedIn's official developer documentation:

- The **Job Posting API** (learn.microsoft.com/linkedin/talent/job-postings) is **write-only**
  — it lets approved ATS/job-distributor partners *post* jobs. It does not search or read.
  It is partner-gated and the overview opens with: *"We are currently not accepting new
  partnerships for LinkedIn's Job Posting API."*
- There is **no official documentation** for `/jobs-guest/jobs/api/...`, the endpoints this
  project uses. They serve the logged-out "see more jobs" experience. Community
  reverse-engineering exists and one such document notes that *"some parameters (like
  `f_WT`) may no longer function as documented"* — independent corroboration of §3 below.

### What the official schema *does* give us

The Job Posting **Foundation Schema** defines the field LinkedIn itself uses:

> `workplaceTypes` — "Represents the workplace nature of the job. Available options are
> `On-site`, `Hybrid`, `Remote`." … *"It is recommended to default set the value of this
> field as `On-site`, if an employer is unsure"*. Required only *"if available on career
> site"*.

And — the load-bearing part for this feature — it ties **location format to work model**:

> `Remote` jobs require either Country only, City and Country, or Country Cluster.
> For `On-site` and `Hybrid` jobs, `location` field should be in either of the following
> formats "CITY, STATE, COUNTRY", "CITY,STATE", "CITY, PROVINCE", "CITY, COUNTRY" or
> "POSTALCODE, COUNTRYCODE"

This is the documented basis for `remote_location_shape` (§5).

It also explains the multi-city duplicates the user reported:

> `alternateLocations` — "Maximum up to **seven** alternate locations are allowed."

**Caveat that must not be lost**: the schema describes what a partner *posts*. The guest
card shows LinkedIn's *display normalization* of it. They are not the same string, so
inferences from card text are correlational. The 10× error-rate spread in §5 is the
evidence, not the document alone.

## 2. Is the work model readable per job without login?

**No.** This was the decisive question and it was tested four ways. All negative.

| Surface | Sample | Result |
|---|---|---|
| Search card HTML | every element class enumerated across all cards, 3 searches | only `base-search-card__title`, `__subtitle`, `job-search-card__location`, `__listdate`, `job-posting-benefits`. **No work-model field.** |
| `jobPosting` guest endpoint | 15 jobs (5 each from `f_WT=1/2/3`) | `description__job-criteria-list` is **always exactly** 4 labels: Nível de experiência, Tipo de emprego, Função, Setores. Never the model. |
| Full public job page | 6 jobs (3 non-remote, 3 remote), ~287–321 KB each | no element whose text is a model word; no `workplaceType`/`workRemoteAllowed`/`jobLocationType`/`remoteAllowed` JSON key; `<title>` and `meta description` carry **location**, never the model. |
| JSON-LD (schema.org) | same 6 jobs + earlier probes | `script types: []` / `['(none)']` — LinkedIn emits **no** `ld+json` here, so `jobLocationType: TELECOMMUTE` is unavailable. |

A fifth, accidental confirmation: in one sample `f_WT=1` (on-site) and `f_WT=3` (hybrid)
returned **identical job IDs**. The guest search does not distinguish on-site from hybrid at
all.

**Conclusion**: the work-model tag the user sees in their browser is rendered by the
authenticated (Voyager) UI. Reaching it requires a logged-in session (`li_at` cookie) —
against LinkedIn's ToS and a risk to the account being used to job-hunt. Ruled out
(spec FR-013). This is *the* reason this feature mitigates rather than fixes.

## 3. `f_WT` (work-model filter) is parsed but intermittently ignored

The user's 219 `Localidade/Modelo incorreto` records are **100% `workplace_type="remoto"`**
— none from the hybrid searches. So the leak is `f_WT=2`.

**Control first** (this is what makes the rest interpretable): the same URL fetched 4×
returned identical IDs *in identical order*. The endpoint is deterministic per URL.

**Paired test**, identical URL with and without `f_WT=2`, 24 samples across 4 keywords ×
3 pages × 2 query shapes (with and without `f_TPR=r86400`):

```
f_WT=2 returned EXACTLY the unfiltered set in 3/24 samples
```

Two disjoint sets cannot coincide by chance in a corpus of thousands, and the control rules
out per-request randomness. Earlier interleaved probes saw the same collision
(`f_WT=2` ∩ `f_WT=3` = 10/10 in one round of three).

The parameter *is* parsed — `f_WT=99` returns 0 results, not everything — so this is the
filter being dropped somewhere in serving, not a syntax error on our side. Nothing on the
client can fix it.

Other verified parameters: `keywords`, `location`, `geoId`, `start`, `f_TPR`
(`r86400`/`r604800`/`r2592000`), `distance`, and comma-lists (`f_WT=1,2,3`). Documented by
the community but unused here: `f_E` (experience), `f_JT` (job type), `f_JIYN` (<10
applicants), `f_AL` (easy apply).

Pagination note: the endpoint returns ~10 cards per page, not the 25 community docs assume.
The existing `start += len(cards)` is correct; a fixed `+25` would skip results.

## 4. `distance` — works, but only where it can

`distance` is a radius in **miles** around `geo_id`.

On a **city** geoId (Florianópolis, `f_WT=3`):

```
distance=None  10 cards | Florianópolis 7, São José 3   | 0 rejected by the hub gate
distance=0      0 cards
distance=25      6 cards | Florianópolis 4, São José 2   | 0 rejected
distance=50     10 cards | Florianópolis 7, São José 3   | 0 rejected
distance=100    10 cards | + Blumenau 4                  | 4 rejected → wasted evaluations
```

So `50` maximizes recall without waste; `100` reaches cities the hub gate then discards.
`0` is honoured as a real radius (returns nothing) — it must be distinguishable from unset.

On a **country** geoId it is a **no-op**. Established with the same control discipline,
because the raw numbers looked like it did something:

```
control pairs (same URL twice) differing:  3/6
test pairs (none vs distance=25) differing: 3/6
```

Identical variation rates ⇒ the differences are endpoint instability, not `distance`. Since
all 219 errors come from the country-geoId remote search, **`distance` cannot reduce them
at all** — which is why it is configured only on the hybrid filters.

**Correction recorded**: an earlier probe suggested omitting `distance` behaves like `25`
(both returned 6 cards). A later probe returned 10 for unset, matching `50`. The implicit
default is **not stable**; code comments and config docs were corrected to say so rather
than assert a value.

### Do not collapse the four hub searches into one

Tempting simplification, tested and rejected:

```
São José + Palhoça + Biguaçu + Florianópolis, separate searches: union = 16 jobs
single Florianópolis geoId + distance=50:                                10 jobs
covered 10/16 — 6 jobs lost
```

## 5. Location shape is the strongest anonymous signal — and unusable as a filter

Applying §1's documented format rule to 3,348 remote history records:

| shape | example | n | flagged wrong | rate |
|---|---|---|---|---|
| `country` | `Brasil` | 1645 | 23 | **1.4%** |
| `metro` | `São Paulo e Região` | 319 | 19 | 6.0% |
| `city_country` | `São Paulo, Brasil` | 148 | 10 | 6.8% |
| `city_state` | `São Paulo, SP` | 1236 | 167 | **13.5%** |

A ~10× spread — the documented rule holds in practice. But as a **gate** it is useless: base
rate is 219/3348 = 6.5%, so best-case precision is 12.4%, and dropping `city_state` would
discard **255 jobs the user viewed or applied to** — roughly 7 good jobs per bad one.
Hence FR-007: ship it as a visible risk label, never a filter.

Signals also measured and rejected as gates:

| signal | catches / 203 residual | recall | hits engaged jobs | precision |
|---|---|---|---|---|
| city/UF named in title (`- SP`, `/RJ`) | 10 | 4.9% | 13 | 9.3% |
| location `city_state` | 153 | 75.4% | 255 | 12.4% |
| either | 154 | 75.9% | 264 | 12.0% |

## 6. Why the learned company blocklist is capped at ~15%

```
block a company after N location errors, only if never engaged:
N=2 → 16 companies, catches 30/203 (14.8%), collateral 0
N=3 →  2 companies, catches  7/203 ( 3.4%), collateral 0
```

The ceiling is in the data, not the rule:

```
160 distinct companies produced the 203 residual errors
errors per company: {1: 128, 2: 24, 3: 5, 4: 2, 6: 1}
→ 128/160 (80%) offend exactly ONCE
```

For 80% of offenders the error *is* the first sighting, so no learned rule can predict it.
Dropping the threshold to 1 adds 112 companies of blast radius to catch errors it could not
have prevented; the README documents this explicitly.

The **engagement veto** is what buys zero collateral. Without it, the same rule
(time-ordered, keyed on company) catches 45/203 but costs **142** jobs the user engaged with.

**Measurement hygiene note**: a "N=1 + never-engaged" variant appears to catch 121/203 with
0 collateral, but that number is hindsight-contaminated — the company is blocked on an error
set that includes the very record being scored. It is not achievable prospectively and was
discarded.

## 7. The irreducible residual

**49 of the 203** trip no signal at all. Examples:

```
[Brasil]            AI Engineer I                              | Arco Educação
[Brasil]            Analista de IA Pleno                       | Dadoteca
[Brasil]            Data Engineer | Databricks & Unity Catalog  | BIX Tecnologia
[São Paulo, Brasil] Cientista de Dados Senior                  | Koin
```

Location in the valid Remote format, title silent on the model, no tag available. On the
anonymous surface these are **indistinguishable** from genuinely remote jobs — the
information does not exist there. The user accepted these as manual work.

## 8. Known measurement gap (now closed going forward)

The history persists **no description** (verified: 0 of 3,522 records), so the description
guard's historical effectiveness was **unmeasurable** — the analysis of which patterns fired
on the 219 flagged jobs returned nothing usable. Worse, `conflicts_with_remote` collapsed two
different `False` cases: "no model signal" vs "on-site signal vetoed by a remote word".

`workplace_evidence` (spec FR-008) records the four hit counts per job so the vetoed case is
identifiable on the next triage round. Storing counts rather than the full description keeps
the history small and avoids persisting large third-party text.

## Sources

- [Job Posting API Overview — LinkedIn | Microsoft Learn](https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/overview?view=li-lts-2026-03)
- [Job Posting Schema (Foundation Schema, `workplaceTypes`, `alternateLocations`) — Microsoft Learn](https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/job-posting-api-schema?view=li-lts-2026-03)
- [LinkedIn API Product Catalog — Talent](https://developer.linkedin.com/product-catalog/talent)
- [LinkedIn Jobs Guest API query parameters (gist, reverse-engineered — explicitly not official)](https://gist.github.com/Diegiwg/51c22fa7ec9d92ed9b5d1f537b9e1107)
- [How to fetch the geo_id parameter for the Job API](https://nubela.co/blog/how-to-fetch-geo_id-parameter-for-the-job-api/)

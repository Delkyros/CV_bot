# Contract: configuration surface

The pipeline's external contract for this feature is its **configuration inputs**: env vars
(`.env`) and the `keywords.yaml` keys. This documents the guaranteed behavior a user can rely on.

## Env var contract

### `SCRAPER_HYBRID_HUB_CITIES`
- **Type**: comma-separated list of city name substrings (accent-insensitive).
- **Default**: `florianopolis,floripa,palhoca,biguacu`
- **Effect**: a hybrid job passes the location gate iff its normalized location contains one of
  these substrings. For the default set, the São José-SC homonym guard applies (accepts
  "São José" only with an SC marker; rejects "…dos Campos"/"…do Rio Preto").
- **Empty/unset** → default.

### `SCRAPER_REMOTE_REJECTED_COUNTRIES`
- **Type**: comma-separated list of country name substrings (accent-insensitive).
- **Default**: the 13 current tokens (see data-model.md).
- **Effect**: a remote job is **rejected** iff its normalized location contains one of these
  substrings; otherwise kept (default-keep).
- **Empty/unset** → default.

### `LLM_OUTPUT_LANGUAGE`
- **Type**: string (a language name the LLM understands, e.g. `Portuguese`, `English`).
- **Default**: `Portuguese`
- **Effect**: the match prompt instructs the model to write `strengths`/`gaps`/`verdict` in this
  language. Does not change the JSON contract, the score, or any drop/keep decision.
- **Empty/unset** → default.

## `keywords.yaml` contract

### `escopo_fora_de_alvo` (optional)
- **Type**: list of `{motivo: string, padrao: regex-string}`.
- **Default source**: the full current DS/ML set, versioned in `keywords.example.yaml`.
- **Effect**: each `padrao` is matched against the normalized job title; a match hard-drops the
  job before any LLM call, logging `Discarded as out of scope by title (<motivo>)`. The effective
  drop set is this seed **unioned and deduplicated** with the learn-from-marks blocklist.
- **Missing/empty** → no seed patterns (learned blocklist still applies); never a crash.
- **Uncompilable `padrao`** → that entry is skipped with a warning; the rest load.

## Invariants (must hold — map to FRs)

1. **Empty `.env` + example config = today's behavior** for every deterministic decision (FR-006).
2. **No knob, when empty or malformed, drops a good job or crashes** (FR-009, Principle II/VI).
3. **Every var/key above is documented** in the README Tunables table with name, default, effect (FR-007).
4. **No user-specific value remains inline** in `.py` source — only as an env default or in the
   example config (FR-008); verifiable by grepping for the city/country/DS-track literals.

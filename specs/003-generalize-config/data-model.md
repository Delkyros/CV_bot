# Phase 1 Data Model: the knobs

No database entities. The "data model" here is the set of configuration knobs and the
one new config structure.

## Environment variables (via `src/settings.py`)

| Var | Helper | Default (reproduces today) | Consumed by |
|-----|--------|----------------------------|-------------|
| `SCRAPER_HYBRID_HUB_CITIES` | `env_list` | `florianopolis, floripa, palhoca, biguacu` | `scraper.workplace_matches` |
| `SCRAPER_REMOTE_REJECTED_COUNTRIES` | `env_list` | `estados unidos, united states, canada, espanha, spain, portugal, india, mexico, argentina, reino unido, republica dominicana, alemanha, franca` | `scraper.workplace_matches` |
| `LLM_OUTPUT_LANGUAGE` | `env_str` | `Portuguese` | `matcher._build_prompt` |

All read lazily at call/run time (never at import — see `settings.py` module docstring),
each with the default above, so an empty `.env` = current behavior.

### `env_list(name, default)` — new helper

- Input: comma-separated string. Split on `,`, `strip()` each item, drop empties.
- If the parsed result is empty (unset, blank, or only separators) → return `default`, log a
  warning **only** when the raw value was non-empty-but-unusable (consistent with the other
  helpers' "invalid → warn + default" contract). Unset/empty → silent default.
- Values are compared after `normalize_text` at the call sites (accent-insensitive), matching
  how `workplace_matches` already normalizes locations.

## Config structure: `escopo_fora_de_alvo` (in `keywords.yaml`)

New optional top-level key. A list of entries:

```yaml
escopo_fora_de_alvo:
  - motivo: "desenvolvedor de BI/Qlik/Power BI"
    padrao: "\\bqlik\\b|power\\s*bi|\\bbi\\b[^.,|]*\\b(developer|desenvolvedor)\\b|..."
  - motivo: "analista de sistemas"
    padrao: "\\banalista de sistemas?\\b"
  # ... (the full current DS/ML set is the versioned default in keywords.example.yaml)
```

**Fields**:
- `motivo` (string, required) — human-readable reason, surfaced in logs and the web UI
  ("Discarded as out of scope by title (<motivo>)"). Portuguese (Principle V).
- `padrao` (string, required) — a regex matched against the accent-stripped, lowercased title.

**Validation / lifecycle**:
- Missing key or empty list → no seed patterns; the deterministic title gate then relies solely
  on the learned blocklist (union source), never crashing (Principle II/VI).
- An entry with an uncompilable `padrao` → skipped with a warning; other entries still load.
- Loaded once per run in `main.py`, compiled, then passed to `out_of_scope_title(title, patterns)`.

## Derived: effective title blocklist (union)

```
effective_drop(title) :=
    out_of_scope_title(title, seed_patterns)   # regex seed from escopo_fora_de_alvo
 OR title_key(title) in learned_scope_blocklist(history)   # exact learned keys
```

- **Union** of two sources (seed regex + learned exact-keys), **deduplicated**: duplicate seed
  entries collapse; a learned key already matched by a seed pattern is redundant (no double drop,
  no error). Matching mechanisms are unchanged from today — only the seed's *origin* moved to YAML.

## Function signature changes

| Function | Before | After |
|----------|--------|-------|
| `text_signals.out_of_scope_title` | `(title)` — uses module constant | `(title, patterns)` — patterns injected |
| `scraper.workplace_matches` | `(location_text, workplace_type)` | unchanged signature; reads env internally |
| `matcher._build_prompt` | `(job_info, candidate_profile, exemplars=None)` | unchanged signature; reads `LLM_OUTPUT_LANGUAGE` internally |

Only `out_of_scope_title` changes signature (its callers: `main.py` + tests).

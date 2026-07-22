# Contract: Offline CLI tools

Two hand-run scripts. Both read `vagas_historico.json` (path from existing history env) read-only
and exit non-zero only on a usage/IO error — never on "not enough data" (that is a reported outcome,
not a failure).

## `eval_scores.py` (US1 — the harness)

```
.venv/Scripts/python.exe eval_scores.py [--refreeze] [--report PATH] [--quiet]
```

| Flag | Effect |
|------|--------|
| (none) | Read history, print report to stdout, write it to `EVAL_REPORT_PATH`. Create the golden-set snapshot if missing. |
| `--refreeze` | Deliberately re-create the golden-set snapshot from the current N most-recent labeled jobs, overwriting the frozen file. |
| `--report PATH` | Override the report output path. |
| `--quiet` | Write the file, suppress stdout. |

**Guarantees**
- Read-only over `vagas_historico.json` (never opens it for write). Verified by a test.
- Deterministic given the same history + golden set (no randomness; temporal split is by timestamp).
- Below `EVAL_MIN_LABELS` overall → prints a single "insufficient labels" report and exits 0.
- Report shape per [data-model.md](../data-model.md#derived-entity-evaluation-report-harness-output).

**Exit codes**: `0` success (including insufficient-data); `2` bad args / history file unreadable.

## `train_meta_model.py` (US3 — the meta-model)

```
.venv/Scripts/python.exe train_meta_model.py [--model PATH] [--report PATH]
```

Requires the analysis extra: `.venv/Scripts/python.exe -m pip install -r requirements-analysis.txt`.

**Guarantees**
- Reads history read-only; writes ONLY `META_MODEL_PATH` + `META_MODEL_REPORT_PATH` (both separate
  from history).
- Below `EVAL_MIN_LABELS` → skips training, writes/prints "insufficient labels", exits 0.
- Reports per-feature coefficients + intercept + temporal-test AUC + Brier + reliability bins
  (see [data-model.md](../data-model.md#new-file-meta-model-artifact--report)).
- Does NOT modify the pipeline or any scoring at runtime (artifact only).

**Exit codes**: `0` success/insufficient; `2` bad args / IO error; `3` `scikit-learn` not installed
(with the pip hint).

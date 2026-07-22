# Contract: Artifacts & the few-shot prompt block

## Golden-set snapshot file

- **Location**: `GOLDEN_SET_PATH` (default `data/golden_set.json`), never the history file.
- **Lifecycle**: created once (first harness run, or `--refreeze`); read-only otherwise.
- **Invariant**: evaluation reads `label` + scores from this file, so re-triaging a job in the web UI
  does NOT move the regression baseline. Schema in [data-model.md](../data-model.md#new-file-golden-set-snapshot).

## Meta-model artifact

- **Location**: `META_MODEL_PATH` (`data/meta_model.joblib`) + `META_MODEL_REPORT_PATH`
  (`data/meta_model_report.json`).
- **Invariant**: existence of these files changes NOTHING at pipeline runtime. No code on the
  `main.py`/`webapp.py` path loads them in this feature. Wiring-in is a separate, later decision
  (explicitly out of scope).

## Few-shot prompt block (the only live-path contract)

`matcher.analyze_match(job_info, candidate_profile, exemplars=None)` and
`matcher._build_prompt(job_info, candidate_profile, exemplars=None)`.

**`exemplars`**: either `None`/empty, or a pre-rendered string block prepared by `main.py`.

**Behavior contract**
- `exemplars` falsy → `_build_prompt` returns the **byte-identical** current prompt. Verified by a
  test comparing to the pre-feature output.
- `exemplars` present → a section is inserted before "Analysis Instructions", of the shape:

  ```
  The candidate has personally reviewed similar postings. Use these as calibration for their taste
  (do NOT copy scores; judge THIS posting on its merits):
  Examples the candidate CHOSE TO APPLY to:
  - <title> @ <company> — <short why, from stored verdict/strengths, truncated>
  Examples the candidate REJECTED as out of scope / irrelevant:
  - <title> @ <company> — <short why, truncated>
  ```

- The block never exceeds `FEWSHOT_CHAR_BUDGET`; exemplars are truncated/dropped to fit.
- The **response contract is unchanged**: the LLM still returns
  `{match_score, core_role_compatible, strengths, gaps, verdict}` and `_parse_result` is untouched.
- No drop/keep decision, threshold, or `core_role_compatible` handling changes — the block only
  informs the score (spec FR-011, Principles II & VI).

**Selection contract (in `main.py`, once per run)**
- Uses `match_labels.label_of` for positives/negatives — same definition as the harness.
- Up to `FEWSHOT_MAX_EXEMPLARS` per class, most recent by `first_seen_at`.
- Gated by `FEWSHOT_ENABLED` and `FEWSHOT_MIN_LABELS`; below the minimum → `None`.
- Wrapped in try/except: any failure logs a warning and yields `None` (Principle VI — a run never
  fails because exemplar selection did).

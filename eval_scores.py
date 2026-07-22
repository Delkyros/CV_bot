"""Offline evaluation harness for match quality (spec 002, User Story 1).

Reads vagas_historico.json READ-ONLY, treats the user's own triage marks as
ground truth (src/match_labels.label_of), and reports how well each of the three
persisted scores — score_gemini, score_vetor_desc, score_title — separates jobs
the user applied to from ones they rejected. Temporal (train-on-past/test-on-
recent) split, per-slice (nearest search term, PT vs EN) breakdown, and a frozen
golden-set regression gate. Prints + writes a markdown report. Stdlib only.

Usage:
    .venv/Scripts/python.exe eval_scores.py [--refreeze] [--report PATH] [--quiet]

Exit codes: 0 success (incl. insufficient data); 2 bad args / unreadable history.
"""
import argparse
import json
import os
import sys
from datetime import datetime

from src.match_labels import label_of, is_probably_pt, nearest_term
from src.settings import env_int, env_float, env_str

SCORES = ("score_gemini", "score_vetor_desc", "score_title")


# --- Metrics (pure Python, no numpy) -----------------------------------------

def auc(pairs):
    """Rank-based AUC (Mann-Whitney). `pairs`: list of (score, is_positive).
    Returns None when a class is empty (not computable)."""
    n_pos = sum(1 for _, p in pairs if p)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    # Average ranks (1-based), ties share the mean rank.
    ordered = sorted(pairs, key=lambda x: x[0])
    ranks = [0.0] * len(ordered)
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][0] == ordered[i][0]:
            j += 1
        avg = (i + j) / 2 + 1  # mean of ranks i+1..j+1
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    sum_pos = sum(r for r, (_, p) in zip(ranks, ordered) if p)
    return (sum_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def suggest_threshold(pairs):
    """Threshold maximizing Youden's J (tpr - fpr). None if a class is empty."""
    n_pos = sum(1 for _, p in pairs if p)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    best_t, best_j = None, -1.0
    for cand, _ in pairs:
        tp = sum(1 for s, p in pairs if p and s >= cand)
        fp = sum(1 for s, p in pairs if not p and s >= cand)
        j = tp / n_pos - fp / n_neg
        if j > best_j:
            best_j, best_t = j, cand
    return best_t


def _median(values):
    vals = sorted(values)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


# --- Records & labels --------------------------------------------------------

def build_records(history):
    """[{label:bool, first_seen_at, job_title, score_*...}] for labeled entries."""
    records = []
    for entry in history.values():
        lab = label_of(entry)
        if lab is None:
            continue
        records.append({
            "label": lab == "pos",
            "first_seen_at": entry.get("first_seen_at", ""),
            "job_title": entry.get("job_title", ""),
            **{s: entry.get(s) for s in SCORES},
        })
    return records


def temporal_split(records, frac):
    """Oldest `frac` by first_seen_at = train, recent remainder = test."""
    ordered = sorted(records, key=lambda r: r["first_seen_at"])
    cut = int(len(ordered) * frac)
    return ordered[:cut], ordered[cut:]


def _pairs(records, score_key):
    """(score, is_pos) for records whose score_key is present (non-None)."""
    return [(float(r[score_key]), r["label"]) for r in records if r.get(score_key) is not None]


# --- Report ------------------------------------------------------------------

def _score_line(records, score_key, min_labels):
    pairs = _pairs(records, score_key)
    n_pos = sum(1 for _, p in pairs if p)
    n_neg = len(pairs) - n_pos
    if len(pairs) < min_labels or n_pos == 0 or n_neg == 0:
        return f"  {score_key:16} insufficient labels (n={len(pairs)}, pos={n_pos}, neg={n_neg}, need {min_labels})"
    a = auc(pairs)
    t = suggest_threshold(pairs)
    pm = _median([s for s, p in pairs if p])
    nm = _median([s for s, p in pairs if not p])
    inv = "  ⚠ INVERTED (neg outscore pos)" if a is not None and a < 0.5 else ""
    return f"  {score_key:16} AUC={a:.3f}  thr={t:.3f}  pos_med={pm:.3f} neg_med={nm:.3f} (n={len(pairs)}){inv}"


def _section(title, records, min_labels):
    lines = [f"### {title}  (labeled n={len(records)})"]
    n_pos = sum(1 for r in records if r["label"])
    if len(records) < min_labels:
        lines.append(f"  insufficient labels (n={len(records)}, need {min_labels})")
        return "\n".join(lines)
    lines.append(f"  positives={n_pos}  negatives={len(records) - n_pos}")
    for s in SCORES:
        lines.append(_score_line(records, s, min_labels))
    return "\n".join(lines)


def render_report(history, search_terms, golden_items, tunables):
    min_labels = tunables["min_labels"]
    frac = tunables["split_frac"]
    records = build_records(history)
    out = ["# Match-quality evaluation report",
           f"_generated {datetime.now().isoformat(timespec='seconds')}_", ""]

    if len(records) < min_labels:
        out.append(f"Insufficient labels overall (n={len(records)}, need {min_labels}). "
                   "Triage more jobs (inscrito / irrelevant / 'Escopo incorreto') and re-run.")
        return "\n".join(out)

    out.append(_section("Overall", records, min_labels))
    out.append("")

    train, test = temporal_split(records, frac)
    out.append("## Temporal (train on past, test on recent)")
    out.append(_section("Train (older)", train, min_labels))
    out.append(_section("Test (recent)", test, min_labels))
    # Drift flag: test AUC materially below train (research D4/B1).
    for s in SCORES:
        at, ax = auc(_pairs(train, s)), auc(_pairs(test, s))
        if at is not None and ax is not None and ax < at - 0.05:
            out.append(f"  ⚠ {s}: test AUC {ax:.3f} < train {at:.3f} − 0.05 (possible drift)")
    out.append("")

    out.append("## Slices")
    # Language slice.
    pt = [r for r in records if is_probably_pt(r["job_title"])]
    en = [r for r in records if not is_probably_pt(r["job_title"])]
    out.append(_section("Language: PT", pt, min_labels))
    out.append(_section("Language: EN", en, min_labels))
    # Nearest-term slice (approximation — see match_labels.nearest_term).
    if search_terms:
        buckets = {}
        for r in records:
            term = nearest_term(r["job_title"], search_terms) or "(none)"
            buckets.setdefault(term, []).append(r)
        for term, recs in sorted(buckets.items()):
            out.append(_section(f"Nearest term: {term}", recs, min_labels))
    out.append("")

    out.append("## Golden set (frozen regression gate)")
    golden_records = [{"label": it["label"] == "pos",
                       "first_seen_at": it.get("first_seen_at", ""),
                       "job_title": "",
                       **{s: it.get(s) for s in SCORES}} for it in golden_items]
    out.append(_section("Golden set", golden_records, min_labels))
    return "\n".join(out)


# --- Golden set --------------------------------------------------------------

def load_or_create_golden(history, path, size, refreeze):
    if not refreeze and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("items", [])
    labeled = [(link, e, label_of(e)) for link, e in history.items() if label_of(e)]
    labeled.sort(key=lambda x: x[1].get("first_seen_at", ""), reverse=True)
    chosen = labeled[:size]
    items = [{"job_link": link, "label": lab, "first_seen_at": e.get("first_seen_at", ""),
              **{s: e.get(s) for s in SCORES}} for link, e, lab in chosen]
    snapshot = {"created_at": datetime.now().isoformat(timespec="seconds"),
                "size": len(items),  # actual count (may be < requested if few labels)
                "criteria": f"{size} most-recent labeled jobs by first_seen_at",
                "items": items}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return items


# --- CLI ---------------------------------------------------------------------

def _load_search_terms():
    config_path = env_str("KEYWORDS_CONFIG_PATH", "config/keywords.yaml")
    if not os.path.exists(config_path):
        return []
    try:
        import yaml
        with open(config_path, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("termos_busca", []) or []
    except Exception:
        return []


def main(argv=None):
    parser = argparse.ArgumentParser(description="Match-quality evaluation harness.")
    parser.add_argument("--refreeze", action="store_true", help="rebuild the golden-set snapshot")
    parser.add_argument("--report", help="report output path (default EVAL_REPORT_PATH)")
    parser.add_argument("--quiet", action="store_true", help="write the file, no stdout")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv()  # pick up HISTORY_PATH etc. from .env, like main.py does

    # The report uses non-ASCII (⚠, →); on a Windows cp1252 console print() would
    # crash. Match the UTF-8 file output.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    history_path = env_str("HISTORY_PATH", "vagas_historico.json")
    try:
        with open(history_path, encoding="utf-8") as f:  # read-only, never opened for write
            history = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Cannot read history '{history_path}': {e}", file=sys.stderr)
        return 2
    if not isinstance(history, dict):
        print(f"History '{history_path}' is not the expected object map.", file=sys.stderr)
        return 2

    tunables = {"min_labels": env_int("EVAL_MIN_LABELS", 20),
                "split_frac": env_float("TEMPORAL_SPLIT_FRAC", 0.7)}
    golden_path = env_str("GOLDEN_SET_PATH", "data/golden_set.json")
    golden = load_or_create_golden(history, golden_path, env_int("GOLDEN_SET_SIZE", 50), args.refreeze)

    report = render_report(history, _load_search_terms(), golden, tunables)

    report_path = args.report or env_str("EVAL_REPORT_PATH", "data/eval_report.md")
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    if not args.quiet:
        print(report)
        print(f"\n(report written to {report_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

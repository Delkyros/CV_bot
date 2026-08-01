"""Derive `escopo_fora_de_alvo` candidates from your own 'Escopo incorreto' marks.

The learned scope blocklist (main.learned_scope_blocklist) matches titles EXACTLY,
so it never generalizes: rejecting "Analista de Dados Sênior" does nothing for
"Analista de Dados Pleno". The config seed `escopo_fora_de_alvo` is the half that
generalizes — this script proposes its entries from the marks you already made,
instead of hand-writing regexes.

Method, mirroring learned_location_blocklist's conservatism (a count threshold plus
an exemption for anything you engaged with):
  * negatives = error_class "Escopo incorreto"           (the roles you rejected)
  * protected = status viewed/applied, OR rejected for CONTRACT/LOCATION reasons
                (those titles were fine as roles — blocking them would be wrong)
  * a candidate n-gram of the title's role portion needs >= --min-count negatives
    and ZERO protected hits, and must not appear in any `termos_busca`.

Nothing is written: it prints the candidates with counts and example titles for you
to approve by hand. Read-only over the history.

Usage:
    .venv/Scripts/python.exe derive_scope_patterns.py [--min-count 5] [--max-n 3]
                                                      [--show-unigrams] [--tail]
"""
import argparse
import collections
import json
import os
import re
import sys

import yaml

from src.match_labels import SCOPE_ERROR_CLASS
from src.settings import env_str
from src.text_signals import normalize_text, title_head, out_of_scope_title

ENGAGED_STATUSES = ("viewed", "applied")
# Rejections that are NOT about the role: the title itself was on-target, so its
# words must never end up in a scope pattern.
ROLE_OK_ERROR_CLASSES = ("Não é CLT", "Localidade/Modelo incorreto")

# Tokens that carry no scope signal on their own. An n-gram made only of these is
# dropped; seniority words are here so "senior"/"ii" never become a pattern.
_STOPWORDS = {
    "de", "da", "do", "das", "dos", "e", "em", "para", "com", "a", "o", "as", "os",
    "and", "of", "the", "for", "in", "at", "to", "or",
    "senior", "sr", "junior", "jr", "pleno", "especialista", "ii", "iii", "iv",
    "i", "vaga", "vagas", "pessoa", "profissional", "remoto", "hibrido",
    "presencial", "home", "office", "clt", "pj", "brasil", "brazil",
}


def _tokens(title, location=""):
    """Scope-bearing tokens of a title's role portion, in order.

    Tokens of the job's OWN `location` are dropped: recruiters glue the city into
    the title with a dash ("Analista de P&D – São José – SC"), which title_head
    cannot cut, and it produced pure-location candidates like 'jose sc'. Using the
    entry's location beats a hardcoded city list — it generalizes to any region.
    """
    place = set(re.findall(r"[^\W\d_]+", normalize_text(location or "")))
    normalized = normalize_text(title_head(title))
    words = re.findall(r"[^\W\d_]+", normalized)  # letters only; drops IDs/punct
    return [w for w in words if len(w) > 1 and w not in place]


def _ngrams(tokens, max_n):
    """All 1..max_n word n-grams usable as a scope pattern.

    An n-gram may not START or END with a stopword: those are sentence fragments,
    not roles, and they match inside legitimate titles — 'de ti' would fire on
    "Engenheiro de Dados de TI", 'pessoa analista' on every job from a company that
    writes inclusive titles, including the ones you want.
    """
    out = set()
    for n in range(1, max_n + 1):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i:i + n]
            if gram[0] in _STOPWORDS or gram[-1] in _STOPWORDS:
                continue
            out.add(" ".join(gram))
    return out


def collect(history):
    """Split the history into (negative, protected) lists of (title, location).

    Reposts are collapsed by (role portion, company): LinkedIn re-publishes the
    same ad with recruiter suffixes ("- Inscrições Abertas", "- Lista de Vagas
    Ativas"), and counting each copy let one spammy company mint a candidate on
    its own. Same reason main.dedupe_jobs exists before the LLM calls.
    """
    negatives, protected = [], []
    seen = set()
    for entry in history.values():
        if not isinstance(entry, dict):
            continue
        title = entry.get("job_title")
        if not title:
            continue
        key = (
            " ".join(normalize_text(title_head(title)).split()),
            normalize_text(entry.get("company") or ""),
        )
        if key in seen:
            continue
        error_class = (entry.get("error_class") or "").strip()
        status = (entry.get("status") or "").strip().lower()
        item = (title, entry.get("location") or "")
        if error_class == SCOPE_ERROR_CLASS:
            seen.add(key)
            negatives.append(item)
        elif status in ENGAGED_STATUSES or error_class in ROLE_OK_ERROR_CLASSES:
            seen.add(key)
            protected.append(item)
    return negatives, protected


def count_ngrams(items, max_n):
    """n-gram -> list of titles containing it (document counts, not term counts)."""
    index = collections.defaultdict(list)
    for title, location in items:
        for gram in _ngrams(_tokens(title, location), max_n):
            index[gram].append(title)
    return index


def rank_candidates(neg_index, pro_index, search_terms, min_count):
    """Candidates that clear the threshold with zero protected collisions.

    Subsumption: a longer n-gram whose negatives are already fully covered by an
    accepted shorter one is dropped — the shorter pattern is broader and enough.
    """
    term_text = " | ".join(normalize_text(t) for t in search_terms)
    survivors = []
    for gram, titles in neg_index.items():
        if len(titles) < min_count:
            continue
        if pro_index.get(gram):
            continue
        if gram in term_text:  # a role you actively search for
            continue
        survivors.append((gram, titles))

    # Broadest first (most negatives, then fewest words), then drop the subsumed.
    survivors.sort(key=lambda kv: (-len(kv[1]), len(kv[0].split()), kv[0]))
    accepted = []
    for gram, titles in survivors:
        covered = set()
        for acc_gram, acc_titles in accepted:
            if all(w in gram.split() for w in acc_gram.split()):
                covered |= set(acc_titles)
        if set(titles) - covered:
            accepted.append((gram, titles))
    return accepted


def seed_patterns(path):
    """Compiled (motivo, regex) pairs from an existing YAML seed, or []."""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    patterns = []
    for entry in config.get("escopo_fora_de_alvo") or []:
        if isinstance(entry, dict) and entry.get("padrao"):
            try:
                patterns.append((entry.get("motivo") or "?", re.compile(entry["padrao"])))
            except re.error:
                pass
    return patterns


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-count", type=int, default=5, help="min 'Escopo incorreto' titles per candidate (default 5)")
    parser.add_argument("--max-n", type=int, default=3, help="longest n-gram considered (default 3)")
    parser.add_argument("--show-unigrams", action="store_true", help="include 1-word candidates (broad — review closely)")
    parser.add_argument("--tail", action="store_true", help="list the rejected titles no candidate covers")
    parser.add_argument("--examples", type=int, default=3, help="example titles printed per candidate (default 3)")
    parser.add_argument("--seed", default="config/keywords.example.yaml", help="existing YAML seed to mark candidates already covered")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()  # pick up HISTORY_PATH etc. from .env, like main.py does

    history_path = env_str("HISTORY_PATH", "vagas_historico.json")
    if not os.path.exists(history_path):
        print(f"History not found: {history_path}", file=sys.stderr)
        return 2
    with open(history_path, encoding="utf-8") as fh:
        history = json.load(fh)

    config_path = env_str("KEYWORDS_CONFIG_PATH", "config/keywords.yaml")
    search_terms = []
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as fh:
            search_terms = (yaml.safe_load(fh) or {}).get("termos_busca") or []

    negatives, protected = collect(history)
    print(f"History: {history_path}  ({len(history)} entries)")
    print(f"  negatives ('{SCOPE_ERROR_CLASS}'): {len(negatives)}")
    print(f"  protected (viewed/applied or contract/location reject): {len(protected)}")
    print(f"  search terms guarded: {len(search_terms)}")
    if not negatives:
        print("\nNothing to derive — no 'Escopo incorreto' marks yet.")
        return 0

    neg_index = count_ngrams(negatives, args.max_n)
    pro_index = count_ngrams(protected, args.max_n)
    accepted = rank_candidates(neg_index, pro_index, search_terms, args.min_count)

    existing = seed_patterns(args.seed)
    multi = [(g, t) for g, t in accepted if len(g.split()) > 1]
    single = [(g, t) for g, t in accepted if len(g.split()) == 1]

    def report(title, rows):
        print(f"\n{'=' * 78}\n{title}  ({len(rows)})\n{'=' * 78}")
        for gram, titles in rows:
            covered = out_of_scope_title(gram, existing) if existing else None
            flag = f"  [already in seed: {covered}]" if covered else ""
            print(f"\n{len(titles):5d}x  {gram!r}{flag}")
            for example in sorted(set(titles))[:args.examples]:
                print(f"         · {title_head(example)}")

    report("MULTI-WORD CANDIDATES (safest — paste these first)", multi)
    if args.show_unigrams:
        report("SINGLE-WORD CANDIDATES (broad — one word blocks a whole family)", single)
    else:
        print(f"\n({len(single)} single-word candidates hidden; --show-unigrams to see them)")

    print(f"\n{'=' * 78}\nYAML for config/keywords.yaml — REVIEW EVERY LINE BEFORE PASTING\n{'=' * 78}")
    print("escopo_fora_de_alvo:")
    for gram, titles in multi:
        # \W+ (not \s+) between words: the n-grams come from a TOKEN stream, so
        # "Engineer (.NET)" yields 'engineer net' — a \s+ pattern would never match
        # the real title. \b anchors keep 'go' off 'golang'.
        padrao = r"\W+".join(re.escape(w) for w in gram.split())
        print(f"  - motivo: '{gram} ({len(titles)} marcações suas)'")
        print(rf"    padrao: '\b{padrao}\b'")

    shown = {t for _, titles in accepted for t in titles}
    print(f"\nCoverage: {len(shown)}/{len(negatives)} rejected titles hit by >=1 candidate.")
    if args.tail:
        print("\nUncovered (one-offs — the exact-match learned blocklist already handles these):")
        for title in sorted({title_head(t) for t in negatives if t not in shown}):
            print(f"  · {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

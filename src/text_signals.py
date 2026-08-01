"""Lightweight text helpers shared across the pipeline.

Contract type (CLT vs PJ/freelancer/internship) is decided entirely here, with
no LLM: `strong_non_clt_evidence` and `internship_evidence` are high-precision
non-CLT signals that override the "assume CLT" default (see
src/matcher.py::classify_contract).
"""

import re
import unicodedata


def normalize_text(text):
    """Lowercase and strip accents/diacritics for accent-insensitive matching."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


# Companies that are contractor / freelance / staffing marketplaces. A posting
# from one of these is almost never a Brazilian CLT employment bond, so it is
# treated as a strong non-CLT signal on its own.
_CONTRACTOR_PLATFORMS = (
    "turing", "crossing hurdles", "hire feed", "toptal", "andela", "crossover",
    "gun.io", "arc.dev", "braintrust", "lemon.io", "x-team", "x team",
    "upwork", "fiverr", "remotask",
)

# High-precision non-CLT cues (PT + EN). Matching any of these is reliable
# enough to OVERRIDE the default-CLT assumption (see matcher.classify_contract).
# Added after auditing misclassified jobs: USD-hourly pay, "Contract:" titles
# and contractor markers (1099/C2C) were the recurring missed signals.
_STRONG_PATTERNS = [
    ("PJ/legal entity", r"\b(pj|pessoa juridica)\b"),
    ("Service provider", r"prestador(?:a)? de servicos|prestacao de servicos"),
    ("Invoice", r"\b(nf|nota fiscal)\b|emitir nota"),
    ("CNPJ/registered company", r"\bcnpj\b|empresa aberta"),
    (
        "Freelancer/self-employed/cooperative",
        r"freelancer|freela|autonomo|cooperado|\bfreelanc\w*|\bself[\s-]?employed\b",
    ),
    ("Hourly rate (PT)", r"valor\s*/?\s*hora|valor hora"),
    # e.g. "$45/hr", "USD 40 per hour", "$ 50 / h"
    ("USD hourly rate", r"(?:\$|usd)\s*\d{1,4}(?:[.,]\d+)?\s*(?:/|per\s*)?\s*(?:hr|hour|hora|h)\b"),
    ("Contractor/1099/C2C", r"\bcontractor\b|\b1099\b|\bc2c\b|corp[\s-]?to[\s-]?corp|\bw-?2\b"),
    # "Contract:" / "Contract -" title convention, or "contract role/position/basis".
    (
        "Contract role",
        r"\bcontract\s*[:\-]|\bcontract\s+(?:role|position|opportunity|basis|to\s+hire|hire|job|work|assignment|worker)\b",
    ),
]


# Internship is a reliable non-CLT marker on its own (an internship is never a
# CLT employment bond). High-precision: "estagi" stem (estágio/estagiário) and
# the explicit English words, with a word boundary so "intern" does not match
# "internal"/"international". "temporário" is deliberately excluded — fixed-term
# CLT contracts exist, so it is not decisive.
_INTERNSHIP_PATTERN = r"\bestagi\w*|\binternship\b|\bintern\b"


def title_head(title):
    """The ROLE portion of a LinkedIn job title, raw (not normalized).

    LinkedIn titles carry noise around the role, and `split("|")[0]` is not enough
    because the noise also comes BEFORE it. Observed in the history:
      "Cientista de Dados | Florianópolis, SC"        -> role first, suffix after '|'
      "|LOJAS RENNER| Assistente de Loja - SHOPPING"  -> bracketed company tag first
      "28446 | Pessoa Analista de Produto II | Remoto" -> requisition ID first
    Taking segment 0 blindly yielded "" for the tag form (the caller then dropped
    the entry entirely) and "28446" for the ID form. So: drop a leading bracketed
    tag, then drop leading segments with no letters, then take the first line.

    ponytail: no all-caps heuristic — a segment like "CAS |" (an internal program
    code) still wins over the real role, but so does a legitimately uppercase
    "ANALISTA DE DADOS | Floripa" title, and breaking the second to fix the first
    is a bad trade (1 case in ~1100 marks). Add one only if the caps form spreads.
    """
    line = (str(title or "").splitlines() or [""])[0]
    # "|TAG| role" — only when the title actually STARTS with '|', so a normal
    # title can never lose its first segment here.
    if line.lstrip().startswith("|"):
        parts = line.lstrip()[1:].split("|", 1)
        line = parts[1] if len(parts) == 2 else parts[0]
    segments = line.split("|")
    for segment in segments:
        if re.search(r"[^\W\d_]", segment):  # any letter, accents included
            return segment.strip()
    return segments[0].strip()


def _match_labels(normalized, patterns):
    return [label for label, pattern in patterns if re.search(pattern, normalized)]


def internship_evidence(text):
    """Return a one-item label list if the text explicitly signals an internship,
    else []. High-precision enough to discard on its own (see classify_contract)."""
    if re.search(_INTERNSHIP_PATTERN, normalize_text(text)):
        return ["Internship/estágio"]
    return []


def contractor_platform(company):
    """Return the marketplace name if `company` is a known contractor platform, else None."""
    normalized = normalize_text(company)
    if not normalized:
        return None
    for name in _CONTRACTOR_PLATFORMS:
        if name in normalized:
            return name
    return None


def strong_non_clt_evidence(text, company=None):
    """High-precision non-CLT signals (USD/hour, contractor, PJ, marketplace company).

    These are reliable enough to justify overriding the default-CLT assumption.
    Returns the matched human-readable labels (empty list when none apply).
    """
    labels = _match_labels(normalize_text(text), _STRONG_PATTERNS)
    platform = contractor_platform(company)
    if platform:
        labels.append(f"Contractor platform: {platform}")
    return labels


def out_of_scope_title(title, patterns):
    """Return a human-readable reason if the job TITLE matches one of the scope
    blocklist `patterns`, else None.

    `patterns` is an iterable of ``(motivo, compiled_regex)`` pairs — the seed
    loaded from config (``escopo_fora_de_alvo``) unioned with the user's learned
    "Escopo incorreto" marks, built once per run in main.py (see
    main.build_scope_patterns). Matched against the accent-stripped, lowercased
    TITLE only: the title is the most reliable scope signal, so clearly off-track
    roles are dropped BEFORE spending an LLM call. Ambiguous titles that overlap
    the candidate's target area are left to the LLM + match-score threshold, NOT
    hard-dropped here. The default seed lives in config/keywords.example.yaml and
    is tuned for a senior DS/ML candidate — replace it per profession.
    """
    normalized = normalize_text(title)
    if not normalized:
        return None
    for reason, regex in patterns:
        if regex.search(normalized):
            return reason
    return None

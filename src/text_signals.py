"""Lightweight text helpers shared across the pipeline.

This module replaces the former embeddings-based contract classifier
(sentence-transformers). The contract type is now inferred by the LLM
(see src/matcher.py::LLMContractClassifier); the keyword logic kept here is
a cheap set of non-CLT signals fed to the LLM as an auditing hint, plus a
high-precision subset that can override the "assume CLT" default outright.
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

# Lower-precision cues used ONLY as an LLM hint (never to override on their own):
# "budget" is too common in legitimate CLT data roles to be decisive.
_HINT_ONLY_PATTERNS = [
    ("Internship/temporary", r"estagio|temporario"),
    ("Budget/billing", r"\bbudget\b|faturamento"),
]


def _match_labels(normalized, patterns):
    return [label for label, pattern in patterns if re.search(pattern, normalized)]


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


def explicit_negative_evidence(text, company=None):
    """All non-CLT signal labels found (strong + hint-only + contractor platform).

    Used as a support hint for the LLM contract classifier — never as the primary
    decision mechanism (that is strong_non_clt_evidence + the LLM).
    """
    normalized = normalize_text(text)
    labels = _match_labels(normalized, _STRONG_PATTERNS) + _match_labels(normalized, _HINT_ONLY_PATTERNS)
    platform = contractor_platform(company)
    if platform:
        labels.append(f"Contractor platform: {platform}")
    return labels

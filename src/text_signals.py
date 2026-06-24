"""Lightweight text helpers shared across the pipeline.

This module replaces the former embeddings-based contract classifier
(sentence-transformers). The contract type is now inferred by the LLM
(see src/matcher.py::LLMContractClassifier); the only keyword logic kept here is
a cheap set of negative signals fed to the LLM as an auditing hint.
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


def explicit_negative_evidence(text):
    """Return human-readable labels for non-CLT signals found in the text.

    Used only as a support hint for the LLM contract classifier — never as the
    primary decision mechanism.
    """
    normalized = normalize_text(text)
    patterns = [
        ("PJ/legal entity", r"\b(pj|pessoa juridica)\b"),
        ("Service provider", r"prestador(?:a)? de servicos|prestacao de servicos"),
        ("Invoice", r"\b(nf|nota fiscal)\b|emitir nota"),
        ("CNPJ/registered company", r"\bcnpj\b|empresa aberta"),
        ("Freelancer/self-employed/cooperative", r"freelancer|freela|autonomo|cooperado"),
        ("Internship/temporary", r"estagio|temporario"),
        ("Hourly rate/budget/billing", r"valor\s*/?\s*hora|valor hora|budget|faturamento"),
    ]
    return [label for label, pattern in patterns if re.search(pattern, normalized)]

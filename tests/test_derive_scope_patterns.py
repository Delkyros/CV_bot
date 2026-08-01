"""Tests for the offline `escopo_fora_de_alvo` derivation (derive_scope_patterns.py).

Only the rules that would silently produce BAD patterns are covered: an n-gram
anchored on a stopword, a location token becoming a "role", a repost inflating a
count, and a protected title failing to veto. The script's real output is read by
hand before anything reaches the YAML.
"""

import derive_scope_patterns as d


def test_ngrams_never_anchor_on_a_stopword():
    grams = d._ngrams(["analista", "de", "suporte"], max_n=3)
    # A stopword in the MIDDLE is fine — the whole phrase is a real role.
    assert "analista de suporte" in grams
    assert {"analista", "suporte"} <= grams
    # Edge-anchored fragments are not roles and match inside good titles.
    assert "analista de" not in grams
    assert "de suporte" not in grams
    assert "de" not in grams


def test_location_tokens_never_become_candidates():
    """The city is glued into the title with a dash, which title_head cannot cut."""
    tokens = d._tokens("Analista de P&D Analítico – São José – SC", "São José, SC")
    assert "jose" not in tokens and "sc" not in tokens
    assert "analista" in tokens and "analitico" in tokens


def test_reposts_are_collapsed_per_company():
    history = {
        "l/1": {"job_title": "Analista de P&D – Inscrições Abertas", "company": "ACME",
                "error_class": "Escopo incorreto", "location": "São José, SC"},
        "l/2": {"job_title": "Analista de P&D – Inscrições Abertas", "company": "ACME",
                "error_class": "Escopo incorreto", "location": "São José, SC"},
        "l/3": {"job_title": "Analista de P&D – Inscrições Abertas", "company": "OUTRA",
                "error_class": "Escopo incorreto", "location": "São José, SC"},
    }
    negatives, _ = d.collect(history)
    assert len(negatives) == 2  # same role+company collapsed; different company kept


def _rank(history, min_count=2, search_terms=()):
    negatives, protected = d.collect(history)
    accepted = d.rank_candidates(
        d.count_ngrams(negatives, 3), d.count_ngrams(protected, 3),
        list(search_terms), min_count,
    )
    return [gram for gram, _ in accepted]


def test_protected_title_vetoes_a_candidate():
    """A role you applied to (or rejected for contract/location) can never be
    proposed as out-of-scope, however many negatives share the word."""
    grams = _rank({
        "l/1": {"job_title": "Engenheiro de Software", "error_class": "Escopo incorreto", "company": "A"},
        "l/2": {"job_title": "Engenheiro de Software Pleno", "error_class": "Escopo incorreto", "company": "B"},
        "l/3": {"job_title": "Engenheiro de Dados", "status": "applied", "company": "C"},
    })
    assert "engenheiro" not in grams  # vetoed by the applied "Engenheiro de Dados"
    # "software" survives and, being broader, SUBSUMES "engenheiro de software":
    # both cover the same two titles, so only the shorter pattern is proposed.
    assert "software" in grams
    assert "engenheiro de software" not in grams


def test_search_term_words_are_never_proposed():
    grams = _rank(
        {
            "l/1": {"job_title": "Cientista de Dados Júnior", "error_class": "Escopo incorreto", "company": "A"},
            "l/2": {"job_title": "Cientista de Dados Trainee", "error_class": "Escopo incorreto", "company": "B"},
        },
        search_terms=["Cientista de Dados"],
    )
    assert "cientista de dados" not in grams
    assert "cientista" not in grams and "dados" not in grams

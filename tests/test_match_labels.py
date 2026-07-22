"""Tests for the shared label definition and the harness metrics (spec 002, US1).

Covers src.match_labels (label_of, is_probably_pt, nearest_term) and the
pure-Python metrics in eval_scores (auc, suggest_threshold, temporal_split).
"""

import pytest

import eval_scores
from src import match_labels


# --- label_of (T003) ---------------------------------------------------------

@pytest.mark.parametrize("entry, expected", [
    ({"status": "applied"}, "pos"),
    ({"status": "Applied"}, "pos"),                        # case-insensitive
    ({"status": "error", "error_class": "Escopo incorreto"}, "neg"),
    ({"error_class": "Escopo incorreto"}, "neg"),
    ({"status": "viewed"}, None),                          # excluded (looked, not applied)
    ({"status": "new"}, None),
    ({"status": "irrelevant"}, None),                      # auto-assigned → excluded (not a user neg)
    ({"status": "duplicado"}, None),
    ({}, None),                                            # untriaged
    ({"error_class": "Localidade/Modelo incorreto"}, None),  # not a role-fit error
    ({"error_class": "Não é CLT"}, None),
    # scope error wins even over an applied status (explicit "wrong role")
    ({"status": "applied", "error_class": "Escopo incorreto"}, "neg"),
])
def test_label_of(entry, expected):
    assert match_labels.label_of(entry) == expected


def test_label_of_non_dict():
    assert match_labels.label_of(None) is None
    assert match_labels.label_of("applied") is None


# --- is_probably_pt -----------------------------------------------------------

def test_is_probably_pt():
    assert match_labels.is_probably_pt("Vaga com experiência em dados")   # diacritic
    assert match_labels.is_probably_pt("Analista de dados para o time")   # PT words
    assert not match_labels.is_probably_pt("Senior Data Scientist role")
    assert match_labels.is_probably_pt("")                                # empty → PT


# --- nearest_term (mock the heavy embedding model) ----------------------------

class _FakeModel:
    def embed(self, text):
        return set(text.lower().split())

    def similarity(self, a, b):
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)  # Jaccard


def test_nearest_term(monkeypatch):
    fake = _FakeModel()
    monkeypatch.setattr("src.local_match._load", lambda: (fake, None))
    monkeypatch.setattr("src.local_match._term_vec", lambda t: fake.embed(t))
    terms = ["cientista de dados", "engenheiro de software"]
    assert match_labels.nearest_term("Cientista de Dados Senior", terms) == "cientista de dados"
    assert match_labels.nearest_term("Engenheiro de Software Backend", terms) == "engenheiro de software"


def test_nearest_term_no_terms():
    assert match_labels.nearest_term("anything", []) is None


# --- auc / suggest_threshold --------------------------------------------------

def test_auc_perfect_separation():
    pairs = [(0.9, True), (0.8, True), (0.2, False), (0.1, False)]
    assert eval_scores.auc(pairs) == 1.0


def test_auc_inverted():
    pairs = [(0.1, True), (0.2, True), (0.8, False), (0.9, False)]
    assert eval_scores.auc(pairs) == 0.0


def test_auc_ties_half():
    # positives and negatives all tied → no separation → 0.5
    pairs = [(0.5, True), (0.5, False)]
    assert eval_scores.auc(pairs) == 0.5


def test_auc_single_class_is_none():
    assert eval_scores.auc([(0.9, True), (0.8, True)]) is None
    assert eval_scores.auc([]) is None


def test_suggest_threshold_separates():
    pairs = [(0.9, True), (0.8, True), (0.2, False), (0.1, False)]
    thr = eval_scores.suggest_threshold(pairs)
    assert 0.2 < thr <= 0.8  # somewhere above the negatives, at/below the positives


# --- temporal_split -----------------------------------------------------------

def test_temporal_split_orders_by_time():
    recs = [{"first_seen_at": f"2026-0{i}-01"} for i in (4, 1, 3, 2)]
    train, test = eval_scores.temporal_split(recs, 0.5)
    assert [r["first_seen_at"] for r in train] == ["2026-01-01", "2026-02-01"]
    assert [r["first_seen_at"] for r in test] == ["2026-03-01", "2026-04-01"]

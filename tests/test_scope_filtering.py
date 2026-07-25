"""Tests for the collection/persist-level scope gate that drops out-of-scope
jobs before they reach the report or the history DB.

Two layers, both default-in-scope (a job is dropped only on an explicit
off-track signal): the deterministic TITLE gate (text_signals.out_of_scope_title)
and the LLM `core_role_compatible` judgment wired through
main.analyze_and_filter_jobs.
"""

import os

import pytest
import yaml

import main
from src import matcher
from src.text_signals import out_of_scope_title

# The versioned default title-scope seed (config/keywords.example.yaml), compiled
# the same way the pipeline does at runtime (main.build_scope_patterns).
_EXAMPLE_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "keywords.example.yaml")
with open(_EXAMPLE_CONFIG, "r", encoding="utf-8") as _f:
    DEFAULT_SCOPE_PATTERNS = main.build_scope_patterns(yaml.safe_load(_f) or {})


# --------------------------------------------------------------------------- #
# LLM core_role_compatible scope gate, wired through
# main.analyze_and_filter_jobs — proves out-of-scope jobs are dropped before
# reaching the report / history DB.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ('{"match_score": 70, "core_role_compatible": false}', False),
    ('{"match_score": 70, "core_role_compatible": "false"}', False),
    ('{"match_score": 70, "core_role_compatible": "não"}', False),
    ('{"match_score": 70, "core_role_compatible": true}', True),
    ('{"match_score": 70}', True),  # omitted -> conservative keep
])
def test_parse_result_extracts_core_role_compatible(raw, expected):
    assert matcher._parse_result(raw)["core_role_compatible"] is expected


# Roles the user flagged "Escopo incorreto" that the LLM should mark incompatible.
OUT_OF_SCOPE_ROLES = [
    "Mobile Android Sênior", "QA Engineer", "Excel Expert",
    "People Analytics", "Analista de Processos II", "Pesquisador II", "Kotlin Engineer",
]
IN_SCOPE_ROLES = ["Machine Learning Engineer", "Cientista de Dados Sênior"]

# Titles that PASS the deterministic title gate (so scope is decided only by the
# LLM/fallback) — used to exercise the infra-failure path without the gate
# short-circuiting first.
NOT_TITLE_GATED = ["Excel Expert", "Analista de Processos II", "Kotlin Engineer"]


def _fake_analyze_match(job, _profile, exemplars=None):
    """Simulate the LLM: out-of-scope roles get core_role_compatible=False."""
    incompatible = job["job_title"] in OUT_OF_SCOPE_ROLES
    return {
        "match_score": 55,
        "core_role_compatible": not incompatible,
        "strengths": [], "gaps": [], "verdict": "ok",
    }


def test_out_of_scope_jobs_are_dropped_before_persist(monkeypatch):
    monkeypatch.setattr(main, "analyze_match", _fake_analyze_match)
    collected = [
        {"job_title": t, "company": "X", "job_link": f"l/{i}"}
        for i, t in enumerate(OUT_OF_SCOPE_ROLES + IN_SCOPE_ROLES)
    ]
    analyzed = main.analyze_and_filter_jobs(collected, "perfil", has_llm_provider=True)
    kept = {j["job_title"] for j in analyzed}
    assert kept == set(IN_SCOPE_ROLES)
    for role in OUT_OF_SCOPE_ROLES:
        assert role not in kept


def test_scope_filter_never_discards_when_llm_unavailable(monkeypatch):
    # With no provider, analyze_match must not even be called; fallback keeps all.
    def _boom(*a, **k):
        raise AssertionError("analyze_match should not run without a provider")
    monkeypatch.setattr(main, "analyze_match", _boom)
    collected = [{"job_title": t, "company": "X", "job_link": f"l/{i}"}
                 for i, t in enumerate(NOT_TITLE_GATED)]
    analyzed = main.analyze_and_filter_jobs(collected, "perfil", has_llm_provider=False)
    assert len(analyzed) == len(collected)  # nothing dropped on infra failure


def test_scope_filter_keeps_job_when_llm_call_raises(monkeypatch):
    # An exception during analysis -> fallback (keep), never an out-of-scope drop.
    def _raise(*a, **k):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(main, "analyze_match", _raise)
    analyzed = main.analyze_and_filter_jobs(
        [{"job_title": "Kotlin Engineer", "company": "X", "job_link": "l/1"}],
        "perfil", has_llm_provider=True,
    )
    assert len(analyzed) == 1


# --------------------------------------------------------------------------- #
# Deterministic TITLE scope gate (text_signals.out_of_scope_title). The real
# out-of-scope roles the user flagged in the web UI, matched by title.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("title", [
    "Desenvolvedor Qlik",
    "Market Intelligence Analyst",
    "Analista de Inteligência de Mercado JR",
    "Analista de sistema",
    "Analista de Sistemas Pleno",
    "Desenvolvedor Junior Node.js - Trabalho Remoto",
    "Associate-Graduate:Developer",
    # Clearly non-technical roles that a loose search surfaces and that leaked
    # through when the LLM scope call was unavailable (the regression this fixes).
    "Cozinheiro", "Cozinheira", "Chef de Cozinha", "Garçom", "Auxiliar de Cozinha",
    "Vendedor", "Vendedora", "Consultor de Vendas", "Representante Comercial",
    "Operador de Caixa", "Repositor", "Motorista", "Entregador", "Motoboy",
    "Eletricista", "Pedreiro", "Enfermeiro", "Técnico de Enfermagem", "Médico",
    "Recepcionista", "Atendente de Loja", "Auxiliar Administrativo", "Telemarketing",
    # Off-track tech/business roles the user flagged "Escopo incorreto": general
    # software/backend/mobile dev, data analyst/BI, HR, sales, QA, logistics,
    # finance, infosec. The candidate's scope is DS/ML/AI/data-engineering only.
    "Backend Engineer", "Desenvolvedor Backend", "Desenvolvedor Back end",
    "Senior AI Backend Engineer", "Pessoa Desenvolvedora Android Sr.",
    "Desenvolvedor(a) Mobile Flutter Sênior", "Programador Delphi SR",
    "Full-stack pleno Laravel, PHP, Vue.js", "Desenvolvedor JAVA+Camunda",
    "Analista de Dados", "Analista de Dados e BI", "Data Analyst", "Web Analytics",
    "Analista de Dados - Trabalho Remoto",
    "HR Business Partner", "Analista de Atração e Seleção",
    "Analista Planejamento Comercial Pleno", "Analista de pré-vendas",
    "Gerente de vendas - imobiliário", "Supervisor de Vendas Externas",
    "Líder De Marketing", "Analista de QA Júnior", "Analista de Controladoria Pleno",
    "Especialista WMS", "Analista de Transportes Júnior", "Analista de Suporte a Franquias",
    "Analista de Segurança da Informação Sr", "Programa de Trainees 2026",
])
def test_out_of_scope_title_flags_off_track_roles(title):
    assert out_of_scope_title(title, DEFAULT_SCOPE_PATTERNS) is not None


@pytest.mark.parametrize("title", [
    "Cientista de Dados Sênior",
    "Machine Learning Engineer",
    "Engenheiro de Dados",
    "AI Developer",
    "Data Insights - Tech Senior Associate",
    # Guards: the new off-track patterns must NOT catch the target DS/ML/AI roles,
    # including ones that share an ambiguous stem (consultor de dados vs analista
    # de dados; engenharia de dados vs analista de dados; software+IA vs backend).
    "Consultor de Dados", "Chief Data Officer", "Gerente de Dados",
    "Cientista de Dados Pleno", "AI Engineer - RAG & Semantic Search",
    "Analista Sênior Engenharia de Dados - Mercado Pago",
    "Engenheiro(a) de Software – IA Generativa e Agentes",
    "Engenheiro(a) de MLOps & Edge Computing (Pleno)",
    "Especialista em Ciências de Dados",
])
def test_out_of_scope_title_keeps_in_or_adjacent_roles(title):
    assert out_of_scope_title(title, DEFAULT_SCOPE_PATTERNS) is None


def test_out_of_scope_title_empty_seed_keeps_everything():
    # An empty/other-profession seed must let a previously-dropped title through
    # (the generalization: no profile-specific hard-drop when the seed is empty).
    assert out_of_scope_title("Analista de Dados", []) is None
    assert out_of_scope_title("Backend Engineer", []) is None


def test_build_scope_patterns_skips_invalid_regex():
    # One uncompilable padrao is skipped (logged); valid entries still load and drop.
    patterns = main.build_scope_patterns({"escopo_fora_de_alvo": [
        {"motivo": "bad", "padrao": "["},          # invalid regex -> skipped
        {"motivo": "vendas", "padrao": r"\bvendedor\b"},
        {"motivo": "dup", "padrao": r"\bvendedor\b"},  # duplicate padrao -> deduped
    ]})
    assert len(patterns) == 1
    assert out_of_scope_title("Vendedor", patterns) == "vendas"


def test_seed_and_learned_union_drops_once(monkeypatch):
    # A title matched by BOTH the seed and the learned blocklist is dropped exactly
    # once (seed gate short-circuits), never reaching the LLM (dedupe, SC-007).
    def _boom(*a, **k):
        raise AssertionError("analyze_match must not run for a union-blocked job")
    monkeypatch.setattr(main, "analyze_match", _boom)
    collected = [{"job_title": "Vendedor", "company": "X", "job_link": "l/1"}]
    analyzed = main.analyze_and_filter_jobs(
        collected, "perfil", has_llm_provider=True,
        scope_blocklist={"vendedor"}, scope_patterns=DEFAULT_SCOPE_PATTERNS)
    assert analyzed == []


def test_out_of_scope_title_drops_before_llm(monkeypatch):
    # Title-gated jobs must be dropped without ever calling the LLM.
    def _boom(*a, **k):
        raise AssertionError("analyze_match must not run for a title-gated job")
    monkeypatch.setattr(main, "analyze_match", _boom)
    collected = [
        {"job_title": "Desenvolvedor Qlik", "company": "X", "job_link": "l/1"},
        {"job_title": "Analista de Inteligência de Mercado", "company": "X", "job_link": "l/2"},
    ]
    analyzed = main.analyze_and_filter_jobs(
        collected, "perfil", has_llm_provider=True, scope_patterns=DEFAULT_SCOPE_PATTERNS)
    assert analyzed == []


# --------------------------------------------------------------------------- #
# Learned scope blocklist: the user's 'Escopo incorreto' marks auto-feed the
# deterministic gate (main.learned_scope_blocklist / analyze_and_filter_jobs).
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Embedding scope gate (title × termos_busca AND description × CV). Drops a job
# only when BOTH signals are weak; a strong title OR a strong description keeps
# it (default-in-scope). Sims are monkeypatched so the test is deterministic and
# never loads the embedding model.
# --------------------------------------------------------------------------- #

def test_embedding_scope_gate_drops_only_when_both_signals_weak(monkeypatch):
    monkeypatch.setattr(main, "analyze_match",
                        lambda *a, **k: {"match_score": 80, "core_role_compatible": True})
    # title_sim, desc_sim per title. Thresholds default to 0.40/0.40.
    sims = {
        "Fora de Escopo":       (0.20, 0.20),  # both weak  -> DROP
        "Cientista de Dados":   (0.70, 0.65),  # strong both -> keep
        "Título torto mas DS":  (0.25, 0.60),  # weak title, strong desc -> keep (rescue)
        "Título bom desc fraca": (0.60, 0.20),  # strong title, weak desc -> keep (rescue)
    }
    monkeypatch.setattr(main, "title_scope_similarity", lambda t, terms: sims[t][0])
    monkeypatch.setattr(main, "description_similarity", lambda job, prof: sims[job["job_title"]][1])
    collected = [{"job_title": t, "company": "X", "job_link": f"l/{i}"}
                 for i, t in enumerate(sims)]
    analyzed = main.analyze_and_filter_jobs(
        collected, "perfil", has_llm_provider=True, search_terms=["Data Scientist"])
    kept = {j["job_title"] for j in analyzed}
    assert "Fora de Escopo" not in kept
    assert kept == {"Cientista de Dados", "Título torto mas DS", "Título bom desc fraca"}


def test_embedding_scope_gate_skipped_without_search_terms(monkeypatch):
    # No search terms -> scope undecidable -> keep even a both-weak job.
    monkeypatch.setattr(main, "analyze_match",
                        lambda *a, **k: {"match_score": 80, "core_role_compatible": True})
    monkeypatch.setattr(main, "title_scope_similarity", lambda t, terms: 0.0)
    monkeypatch.setattr(main, "description_similarity", lambda job, prof: 0.0)
    analyzed = main.analyze_and_filter_jobs(
        [{"job_title": "Qualquer Coisa", "company": "X", "job_link": "l/1"}],
        "perfil", has_llm_provider=True)  # search_terms defaults to None
    assert len(analyzed) == 1


def test_learned_blocklist_derives_normalized_role_keys():
    history = {
        "l/1": {"job_title": "Designer Gráfico", "error_class": "Escopo incorreto"},
        # City/work-model suffix must be stripped so a repost matches.
        "l/2": {"job_title": "Gerente de Loja | Florianópolis\nFull-time", "error_class": "Escopo incorreto"},
        "l/3": {"job_title": "Cientista de Dados", "error_class": "Não é CLT"},  # other class -> ignored
        "l/4": {"job_title": "Data Scientist"},  # unmarked -> ignored
    }
    bl = main.learned_scope_blocklist(history)
    assert bl == {"designer grafico", "gerente de loja"}


def test_learned_blocklist_drops_reposts_before_llm(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("analyze_match must not run for a learned-blocked job")
    monkeypatch.setattr(main, "analyze_match", _boom)
    blocklist = {"analista de suporte junior"}
    collected = [
        # Same role reposted under a new link + city suffix -> dropped, no LLM.
        {"job_title": "Analista de Suporte Júnior | Curitiba", "company": "Y", "job_link": "l/9"},
        # Not on the blocklist and not title-gated -> would go to the LLM (kept).
        {"job_title": "Cientista de Dados Sênior", "company": "Y", "job_link": "l/10"},
    ]
    monkeypatch.setattr(main, "analyze_match", lambda *a, **k: {"match_score": 80, "core_role_compatible": True})
    analyzed = main.analyze_and_filter_jobs(collected, "perfil", has_llm_provider=True, scope_blocklist=blocklist)
    assert {j["job_title"] for j in analyzed} == {"Cientista de Dados Sênior"}

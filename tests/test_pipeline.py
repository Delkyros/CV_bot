"""Unit tests for the pure (network-free, LLM-free) logic of the pipeline."""

import json

import pytest

from src import scraper, matcher, reporter, text_signals, settings
import main


# --------------------------------------------------------------------------- #
# text_signals
# --------------------------------------------------------------------------- #
def test_normalize_text_strips_accents_and_lowercases():
    assert text_signals.normalize_text("São Paulo") == "sao paulo"
    assert text_signals.normalize_text("") == ""
    assert text_signals.normalize_text(None) == ""


def test_strong_non_clt_evidence_detects_pj_signals():
    signals = text_signals.strong_non_clt_evidence(
        "Contrato PJ, necessário CNPJ ativo e emissão de nota fiscal."
    )
    assert "PJ/legal entity" in signals
    assert "CNPJ/registered company" in signals
    assert "Invoice" in signals


def test_strong_non_clt_evidence_empty_for_clt():
    assert text_signals.strong_non_clt_evidence("Vaga efetiva com carteira assinada.") == []


# --------------------------------------------------------------------------- #
# scraper
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("period,expected", [
    ("24h", "r86400"),
    ("semana", "r604800"),
    ("mes", "r2592000"),
    ("", None),
    (None, None),
])
def test_linkedin_time_filter(period, expected):
    assert scraper.linkedin_time_filter(period) == expected


@pytest.mark.parametrize("model,expected", [
    ("remoto", "2"),
    ("hibrido", "3"),
    ("presencial", "1"),
    ("qualquer", None),
])
def test_linkedin_workplace_filter(model, expected):
    assert scraper.linkedin_workplace_filter(model) == expected


def test_workplace_matches_remote_rejects_foreign():
    assert scraper.workplace_matches("Campinas, SP", "remoto") is True
    assert scraper.workplace_matches("United States", "remoto") is False


def test_workplace_matches_hybrid_only_sc_cities():
    assert scraper.workplace_matches("Florianópolis, SC", "hibrido") is True
    assert scraper.workplace_matches("São Paulo, SP", "hibrido") is False


def test_workplace_matches_hybrid_accepts_sao_jose_sc():
    assert scraper.workplace_matches("São José, Santa Catarina, Brasil", "hibrido") is True
    assert scraper.workplace_matches("São José, SC", "hibrido") is True


def test_workplace_matches_hybrid_rejects_sao_jose_sp_homonyms():
    # "São José dos Campos" / "do Rio Preto" (São Paulo) are DISTINCT city names,
    # not the hub "São José" — the whole-component match rejects them without any
    # city-specific homonym hardcode.
    assert scraper.workplace_matches("São José dos Campos, São Paulo, Brasil", "hibrido") is False
    assert scraper.workplace_matches("São José do Rio Preto, SP", "hibrido") is False


def test_workplace_matches_hybrid_matches_city_as_whole_component():
    # A bare "São José" is an exact match for the configured hub city, so it now
    # passes (the whole-component match replaced the old SC-marker special-case).
    assert scraper.workplace_matches("São José", "hibrido") is True
    # But a longer name that merely starts with a hub city is a different city.
    assert scraper.workplace_matches("São José dos Pinhais, PR", "hibrido") is False


def test_workplace_matches_hybrid_accepts_palhoca_and_biguacu():
    assert scraper.workplace_matches("Palhoça, Santa Catarina, Brasil", "hibrido") is True
    assert scraper.workplace_matches("Biguaçu, SC", "hibrido") is True


def test_workplace_matches_hybrid_rejects_other_sc_cities():
    # Only the Grande Florianópolis hub cities are wanted, NOT the whole state.
    assert scraper.workplace_matches("Criciúma, SC", "hibrido") is False
    assert scraper.workplace_matches("Joinville, SC", "hibrido") is False
    assert scraper.workplace_matches("Mafra, SC", "hibrido") is False
    # A bare state with no target city is too vague -> reject.
    assert scraper.workplace_matches("Santa Catarina, Brasil", "hibrido") is False


def test_workplace_matches_hybrid_cities_configurable_via_env(monkeypatch):
    # A user in another region overrides the hub cities; Florianópolis is no
    # longer a hub, the configured city is.
    monkeypatch.setenv("SCRAPER_HYBRID_HUB_CITIES", "são paulo, guarulhos")
    assert scraper.workplace_matches("São Paulo, SP", "hibrido") is True
    assert scraper.workplace_matches("Guarulhos, São Paulo", "hibrido") is True
    assert scraper.workplace_matches("Florianópolis, SC", "hibrido") is False


def test_workplace_matches_remote_rejected_countries_configurable_via_env(monkeypatch):
    # Overriding the blocklist changes which remote locations pass: the US is no
    # longer rejected, Portugal now is.
    monkeypatch.setenv("SCRAPER_REMOTE_REJECTED_COUNTRIES", "portugal")
    assert scraper.workplace_matches("United States", "remoto") is True
    assert scraper.workplace_matches("Lisboa, Portugal", "remoto") is False
    assert scraper.workplace_matches("Campinas, SP", "remoto") is True


def test_description_conflicts_with_remote_flags_explicit_hybrid_onsite():
    # LinkedIn's f_WT=2 leaks hybrid/on-site jobs; an explicit declaration with
    # no remote option must be flagged (real case: hybrid São Paulo job tagged
    # "remoto" / "Localidade/Modelo incorreto").
    assert scraper.description_conflicts_with_remote(
        "The work location of this role is hybrid, both from home and a LinkedIn office."
    ) is True
    assert scraper.description_conflicts_with_remote("Vaga 100% presencial em nosso escritório.") is True
    assert scraper.description_conflicts_with_remote("Modelo híbrido, 3 dias no escritório.") is True


def test_description_conflicts_with_remote_is_conservative():
    # A remote possibility vetoes the guard; silence about the model does too.
    assert scraper.description_conflicts_with_remote("Trabalho remoto ou híbrido, você escolhe.") is False
    assert scraper.description_conflicts_with_remote("100% remoto, home office.") is False
    assert scraper.description_conflicts_with_remote("Ótima vaga de analista, sem menção a modelo.") is False
    assert scraper.description_conflicts_with_remote("") is False


def test_job_is_closed_detects_banner_and_phrases():
    from bs4 import BeautifulSoup
    closed_text = BeautifulSoup("<div>No longer accepting applications</div>", "html.parser")
    closed_pt = BeautifulSoup("<div>Esta vaga não aceita mais candidaturas.</div>", "html.parser")
    closed_class = BeautifulSoup('<figure class="closed-job"></figure>', "html.parser")
    open_job = BeautifulSoup("<div>Estamos contratando! Candidate-se já.</div>", "html.parser")
    assert scraper.job_is_closed(closed_text) is True
    assert scraper.job_is_closed(closed_pt) is True
    assert scraper.job_is_closed(closed_class) is True
    assert scraper.job_is_closed(open_job) is False


def test_workplace_matches_no_filter():
    assert scraper.workplace_matches("anywhere", None) is True


@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/jobs/view/1234567890", "1234567890"),
    ("https://www.linkedin.com/jobs/search?currentJobId=987654", "987654"),
    ("https://www.linkedin.com/jobs/view/some-title-555111", "555111"),
])
def test_extract_job_id(url, expected):
    assert scraper.extract_job_id(url) == expected


# --------------------------------------------------------------------------- #
# matcher
# --------------------------------------------------------------------------- #
def test_parse_result_clamps_and_normalizes():
    raw = '{"match_score": 150, "strengths": ["x"], "gaps": [], "verdict": "ok"}'
    result = matcher._parse_result(raw)
    assert result["match_score"] == 100
    assert result["strengths"] == ["x"]
    assert result["verdict"] == "ok"


def test_parse_result_strips_code_fences():
    raw = '```json\n{"match_score": 42, "strengths": [], "gaps": [], "verdict": "v"}\n```'
    assert matcher._parse_result(raw)["match_score"] == 42


def test_parse_result_invalid_returns_none():
    assert matcher._parse_result(None) is None
    assert matcher._parse_result("not json at all") is None


def test_contract_local_default_clt_passes_report_bar():
    # No non-CLT signal -> kept as CLT with a numeric score_clt above the report
    # bar (a "N/A" here would silently hide every kept job from the report).
    result = matcher.classify_contract("Vaga efetiva de cientista de dados.", title="DS", company="ACME")
    assert result["accepted"] is True
    assert result["inferred_contract_type"] == "CLT"
    assert float(result["score_clt"]) >= reporter.min_clt_score()


def test_contract_local_discards_internship():
    result = matcher.classify_contract("Vaga de estágio em dados.", title="Estagiário de Dados", company="ACME")
    assert result["accepted"] is False
    assert result["inferred_contract_type"] == "ESTAGIO"


# --------------------------------------------------------------------------- #
# reporter
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("score_clt,match_score,expected", [
    (0.7, 70, True),       # both at the threshold (CLT >= 0.7, match >= 70)
    (0.9, 80, True),       # comfortably above
    (0.69, 90, False),     # CLT below threshold
    (0.8, 69, False),      # profile below threshold
    ("N/A", 90, False),    # non-numeric CLT score is excluded
    (None, 90, False),     # missing CLT score is excluded
    (0.8, None, False),    # missing match score treated as 0
])
def test_passes_relevance_filter(score_clt, match_score, expected):
    job = {"score_clt": score_clt, "match_score": match_score}
    assert reporter.passes_relevance_filter(job) is expected


# --------------------------------------------------------------------------- #
# settings (env-backed tunables)
# --------------------------------------------------------------------------- #
def test_env_float_default_and_invalid(monkeypatch):
    monkeypatch.delenv("X_FLOAT", raising=False)
    assert settings.env_float("X_FLOAT", 0.6) == 0.6
    monkeypatch.setenv("X_FLOAT", "not-a-number")
    assert settings.env_float("X_FLOAT", 0.6) == 0.6
    monkeypatch.setenv("X_FLOAT", "0.8")
    assert settings.env_float("X_FLOAT", 0.6) == 0.8


def test_env_int_default_and_invalid(monkeypatch):
    monkeypatch.setenv("X_INT", "x")
    assert settings.env_int("X_INT", 5) == 5
    monkeypatch.setenv("X_INT", "9")
    assert settings.env_int("X_INT", 5) == 9


def test_report_thresholds_read_from_env(monkeypatch):
    monkeypatch.setenv("REPORT_MIN_CLT_SCORE", "0.9")
    monkeypatch.setenv("REPORT_MIN_MATCH_SCORE", "70")
    # A job that passes the defaults (0.6/50) must now fail the stricter env values.
    job = {"score_clt": 0.8, "match_score": 60}
    assert reporter.passes_relevance_filter(job) is False


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def test_dedupe_jobs_collapses_same_title_company():
    jobs = [
        {"job_title": "AI Engineer", "company": "ACME", "location": "SP", "job_link": "1"},
        {"job_title": "ai engineer", "company": "acme", "location": "RJ", "job_link": "2"},
        {"job_title": "Data Scientist", "company": "ACME", "location": "SP", "job_link": "3"},
    ]
    unique = main.dedupe_jobs(jobs)
    assert len(unique) == 2
    assert unique[0]["job_link"] == "1"


def test_prettify_label_keeps_acronyms_and_connectors():
    assert main._prettify_label("ia_e_llms") == "IA e LLMs"
    assert main._prettify_label("engenharia_de_dados") == "Engenharia de Dados"


def test_format_candidate_profile_renders_sections():
    profile = {
        "nivel_experiencia": "Sênior",
        "resumo_profissional": "Resumo aqui.",
        "competencias_tecnicas": {"linguagens_e_backend": ["Python", "SQL"]},
        "soft_skills": ["Comunicação"],
    }
    text = main.format_candidate_profile(profile)
    assert "Professional Summary:" in text
    assert "Python, SQL" in text
    assert "Experience Level: Sênior" in text


def test_format_candidate_profile_passthrough_string():
    assert main.format_candidate_profile("plain text") == "plain text"


def test_save_job_history_preserves_user_status(tmp_path):

    history_path = tmp_path / "hist.json"
    # Pre-existing entry already marked via the web UI.
    history_path.write_text(json.dumps({
        "https://job/1": {
            "job_title": "Old title", "match_score": 40,
            "first_seen_at": "2026-01-01T00:00:00",
            "status": "applied", "notes": "candidatei-me",
            "status_updated_at": "2026-02-02T00:00:00",
        }
    }), encoding="utf-8")

    analyzed = [{
        "job_link": "https://job/1", "job_title": "New title",
        "company": "ACME", "match_score": 88, "score_clt": 0.9,
    }]
    assert main.save_job_history(str(history_path), {}, analyzed) is True

    saved = json.loads(history_path.read_text(encoding="utf-8"))
    entry = saved["https://job/1"]
    # Scrape-derived fields refreshed...
    assert entry["job_title"] == "New title"
    assert entry["match_score"] == 88
    # ...first_seen_at and user status/notes preserved.
    assert entry["first_seen_at"] == "2026-01-01T00:00:00"
    assert entry["status"] == "applied"
    assert entry["notes"] == "candidatei-me"


def test_save_job_history_flags_sub_bar_jobs_irrelevant(tmp_path):

    history_path = tmp_path / "hist.json"
    analyzed = [
        # Above the bar -> stays a normal "new" job (no explicit status).
        {"job_link": "https://job/ok", "job_title": "DS", "company": "A",
         "match_score": 85, "score_clt": 0.9},
        # Below on match -> flagged irrelevant.
        {"job_link": "https://job/lowmatch", "job_title": "DS", "company": "A",
         "match_score": 55, "score_clt": 0.9},
        # N/A CLT -> flagged irrelevant.
        {"job_link": "https://job/nacl", "job_title": "DS", "company": "A",
         "match_score": 85, "score_clt": "N/A"},
    ]
    assert main.save_job_history(str(history_path), {}, analyzed) is True
    saved = json.loads(history_path.read_text(encoding="utf-8"))

    assert saved["https://job/ok"].get("status") != "irrelevant"
    assert saved["https://job/lowmatch"]["status"] == "irrelevant"
    assert saved["https://job/nacl"]["status"] == "irrelevant"


def test_save_job_history_never_overrides_user_status_with_irrelevant(tmp_path):

    history_path = tmp_path / "hist.json"
    # User already applied to a sub-bar job — must NOT be flagged irrelevant.
    history_path.write_text(json.dumps({
        "https://job/1": {"job_title": "X", "status": "applied"}
    }), encoding="utf-8")
    analyzed = [{"job_link": "https://job/1", "job_title": "X", "company": "A",
                 "match_score": 40, "score_clt": "N/A"}]
    assert main.save_job_history(str(history_path), {}, analyzed) is True
    saved = json.loads(history_path.read_text(encoding="utf-8"))
    assert saved["https://job/1"]["status"] == "applied"


def test_save_job_history_repost_supersedes_untriaged_as_duplicado(tmp_path):

    history_path = tmp_path / "hist.json"
    # Same ad (title+company) already known under two older links: one still
    # untriaged, one the user applied to.
    history_path.write_text(json.dumps({
        "https://job/old-new": {"job_title": "AI Engineer", "company": "CI&T"},
        "https://job/old-applied": {"job_title": "AI Engineer", "company": "CI&T",
                                    "status": "applied"},
    }), encoding="utf-8")

    analyzed = [{"job_link": "https://job/repost", "job_title": "AI Engineer",
                 "company": "CI&T", "match_score": 85, "score_clt": 0.9}]
    assert main.save_job_history(str(history_path), {}, analyzed) is True
    saved = json.loads(history_path.read_text(encoding="utf-8"))

    # The fresh repost is the record to triage; the stale untriaged copy left
    # the queue as "duplicado"; the user-triaged copy is untouched.
    assert saved["https://job/repost"].get("status") != "duplicado"
    assert saved["https://job/old-new"]["status"] == "duplicado"
    assert saved["https://job/old-applied"]["status"] == "applied"


def test_triaged_title_company_keys_ignores_untriaged():
    history = {
        "https://job/1": {"job_title": "DS", "company": "A", "status": "applied"},
        "https://job/2": {"job_title": "MLE", "company": "B", "status": "new"},
        "https://job/3": {"job_title": "DE", "company": "C"},
    }
    assert main.triaged_title_company_keys(history) == {("ds", "a")}


# --------------------------------------------------------------------------- #
# webapp (Flask UI)
# --------------------------------------------------------------------------- #
@pytest.fixture
def web_client(tmp_path, monkeypatch):
    import webapp

    history_path = tmp_path / "hist.json"
    history_path.write_text(json.dumps({
        "https://job/a": {"job_title": "A", "match_score": 70, "score_clt": 0.9},
        "https://job/b": {"job_title": "B", "match_score": 90, "score_clt": 0.8},
    }), encoding="utf-8")
    monkeypatch.setenv("HISTORY_PATH", str(history_path))
    webapp.app.config.update(TESTING=True)
    return webapp.app.test_client(), history_path


def test_api_jobs_sorted_by_score_desc(web_client):
    client, _ = web_client
    data = client.get("/api/jobs").get_json()
    assert data["count"] == 2
    assert [j["match_score"] for j in data["jobs"]] == [90, 70]


def test_api_status_persists(web_client):

    client, history_path = web_client
    resp = client.post("/api/status", json={
        "link": "https://job/a", "status": "applied", "notes": "ok",
    })
    assert resp.status_code == 200
    saved = json.loads(history_path.read_text(encoding="utf-8"))
    assert saved["https://job/a"]["status"] == "applied"
    assert saved["https://job/a"]["notes"] == "ok"
    assert "status_updated_at" in saved["https://job/a"]


def test_api_status_rejects_invalid_status(web_client):
    client, _ = web_client
    resp = client.post("/api/status", json={"link": "https://job/a", "status": "bogus"})
    assert resp.status_code == 400


def test_api_status_unknown_link_404(web_client):
    client, _ = web_client
    resp = client.post("/api/status", json={"link": "https://job/zzz", "status": "viewed"})
    assert resp.status_code == 404


def test_api_status_requires_link(web_client):
    client, _ = web_client
    resp = client.post("/api/status", json={"status": "viewed"})
    assert resp.status_code == 400

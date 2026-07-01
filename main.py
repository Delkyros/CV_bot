import os
import sys
import json
import logging
from datetime import datetime
import yaml
from dotenv import load_dotenv

# Ensure the src/ package files can be imported correctly
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from src.scraper import scrape_linkedin_jobs, normalize_text
from src.text_signals import out_of_scope_title
from src.matcher import analyze_match, has_provider, LLMContractClassifier, min_discard_confidence
from src.reporter import generate_report, passes_relevance_filter, min_clt_score, min_match_score
from src.logging_config import setup_logging
from src.settings import env_str

logger = logging.getLogger(__name__)


# NOTE: these maps drive _prettify_label, which receives the Portuguese skill
# category keys from config/keywords.yaml (e.g. ia_e_llms), so the acronyms and
# connectors are intentionally kept in Portuguese.
_LABEL_ACRONYMS = {
    "ia": "IA", "llm": "LLM", "llms": "LLMs", "nlp": "NLP", "ml": "ML",
    "mlops": "MLOps", "api": "API", "apis": "APIs", "etl": "ETL", "elt": "ELT",
    "aws": "AWS", "gcp": "GCP", "ci": "CI", "cd": "CD",
}
_LABEL_CONNECTORS = {"e", "de", "da", "do", "para", "com"}


def _prettify_label(key):
    """Turn a snake_case key (e.g. ia_e_llms) into a readable label (e.g. 'IA e
    LLMs'), preserving acronyms and keeping connectors lowercase."""
    words = []
    for w in str(key).replace("_", " ").split():
        lower = w.lower()
        if lower in _LABEL_ACRONYMS:
            words.append(_LABEL_ACRONYMS[lower])
        elif lower in _LABEL_CONNECTORS:
            words.append(lower)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def format_candidate_profile(profile):
    """
    Convert the candidate profile (structured dict from the YAML, or plain text
    for backward compatibility) into a readable running text for the matcher
    prompt. The LLM receives formatted text, not the repr of a dict.

    NOTE: the profile dict keys come from config/keywords.yaml and are kept in
    Portuguese on purpose (that file is not translated).
    """
    if profile is None:
        return ""
    if isinstance(profile, str):
        return profile.strip()
    if not isinstance(profile, dict):
        return str(profile).strip()

    sections = []

    summary = profile.get("resumo_profissional")
    if summary:
        sections.append("Professional Summary:\n" + str(summary).strip())

    skills = profile.get("competencias_tecnicas")
    if isinstance(skills, dict):
        skill_lines = ["Technical Skills (Hard Skills):"]
        for category, items in skills.items():
            title = _prettify_label(category)
            if isinstance(items, (list, tuple)):
                items_txt = ", ".join(str(i) for i in items)
            else:
                items_txt = str(items)
            skill_lines.append(f"- {title}: {items_txt}")
        sections.append("\n".join(skill_lines))
    elif skills:
        sections.append("Technical Skills (Hard Skills):\n" + str(skills).strip())

    soft_skills = profile.get("soft_skills")
    if isinstance(soft_skills, (list, tuple)):
        soft_lines = ["Soft Skills:"] + [f"- {s}" for s in soft_skills]
        sections.append("\n".join(soft_lines))
    elif soft_skills:
        sections.append("Soft Skills:\n" + str(soft_skills).strip())

    level = profile.get("nivel_experiencia")
    if level:
        sections.append(f"Experience Level: {level}")

    return "\n\n".join(sections).strip()


def dedupe_jobs(jobs):
    """
    Remove near-duplicates (same title + company, ignoring location/ID) before
    sending to the LLM. LinkedIn often reposts the same job with different IDs in
    several cities; without this, each copy would consume an LLM call. Keeps the
    first occurrence (which already has the downloaded description).

    Dedup by exact link/ID already happens during collection; here we handle the
    reposts with distinct IDs.
    """
    seen = set()
    unique = []
    for job in jobs:
        key = (
            normalize_text(job.get("job_title", "")),
            normalize_text(job.get("company", "")),
        )
        if key in seen:
            logger.info(
                "Duplicate (same title+company) discarded before match: "
                f"{job.get('job_title')} | {job.get('company')} | {job.get('location')}"
            )
            continue
        seen.add(key)
        unique.append(job)
    return unique


def load_job_history(history_path):
    if not os.path.exists(history_path):
        return {}

    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning(f"Could not load history '{history_path}': {e}")

    return {}


# User-owned fields set via the web UI (webapp.py). The pipeline must carry
# these over verbatim so a new search run never wipes "viewed/applied/notes".
USER_STATUS_FIELDS = ("status", "notes", "status_updated_at", "error_class")


def save_job_history(history_path, history, analyzed_jobs):
    now = datetime.now().isoformat(timespec="seconds")

    # Reload from disk so any status/notes set via the web UI *during* this run
    # (which started minutes ago with a now-stale in-memory copy) are not lost.
    # Disk is authoritative for existing entries; fall back to the in-memory
    # copy only if the file could not be read.
    merged = load_job_history(history_path) or dict(history)

    for job in analyzed_jobs:
        link = job.get("job_link")
        if not link:
            continue

        prior = merged.get(link, {})
        entry = {
            "job_title": job.get("job_title", "N/A"),
            "company": job.get("company", "N/A"),
            "location": job.get("location", "N/A"),
            "contract_type": job.get("contract_type", "N/A"),
            "inferred_contract_type": job.get("inferred_contract_type", "N/A"),
            "score_clt": job.get("score_clt", "N/A"),
            "score_non_clt": job.get("score_non_clt", "N/A"),
            "contract_margin": job.get("contract_margin", "N/A"),
            "contract_evidence": job.get("contract_evidence", "N/A"),
            "workplace_type": job.get("workplace_type", "N/A"),
            "match_score": job.get("match_score", 0),
            "first_seen_at": prior.get("first_seen_at", now),
            "last_processed_at": now,
        }
        # Preserve user-set status/notes from the web UI.
        for field in USER_STATUS_FIELDS:
            if field in prior:
                entry[field] = prior[field]

        merged[link] = entry

    try:
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        logger.info(f"History updated at '{history_path}' ({len(merged)} known jobs).")
        return True
    except Exception as e:
        logger.error(f"Failed to save history '{history_path}': {e}")
        return False


def _fallback_analysis(has_llm_provider):
    """Neutral simulated analysis so the pipeline never breaks when the LLM is
    unavailable or fails. core_role_compatible defaults True: we never discard a
    job as out-of-scope on an infrastructure failure."""
    if not has_llm_provider:
        logger.warning("Using simulated fallback analysis: no LLM provider configured (OPENROUTER_API_KEY/GEMINI_API_KEY).")
    else:
        logger.warning("Using simulated fallback analysis due to failure of all LLM providers.")
    return {
        "match_score": 50,  # Neutral score
        "core_role_compatible": True,
        "strengths": ["Job collected successfully", "Available for evaluation"],
        "gaps": ["LLM analysis unavailable (check OPENROUTER_API_KEY/GEMINI_API_KEY in .env)"],
        "verdict": "Set a valid OPENROUTER_API_KEY and/or GEMINI_API_KEY in the .env file to get a real AI analysis of this job.",
    }


def analyze_and_filter_jobs(collected_jobs, candidate_profile, has_llm_provider):
    """Run the match analysis on each collected job and DROP the ones whose core
    role does not match the candidate's area (scope filter), so they never reach
    the report or the history DB.

    Returns the list of in-scope analyzed jobs (job data merged with the analysis).
    """
    analyzed_jobs = []
    total_jobs = len(collected_jobs)

    for idx, job in enumerate(collected_jobs, start=1):
        logger.info(f"Analyzing job {idx} of {total_jobs}: {job['job_title']} | Company: {job['company']}")

        # Deterministic title-based scope gate: the job TITLE is the most
        # reliable scope signal, so drop clearly out-of-track roles (BI/Qlik
        # dev, market intelligence/research, systems analyst, generic Node/
        # graduate dev) BEFORE spending an LLM call. Ambiguous titles fall
        # through to the LLM's core_role_compatible judgment below.
        out_reason = out_of_scope_title(job.get("job_title"))
        if out_reason:
            logger.info(
                "Discarded as out of scope by title "
                f"({out_reason}): {job['job_title']} | {job['company']}"
            )
            continue

        analysis_result = None
        if has_llm_provider:
            try:
                analysis_result = analyze_match(job, candidate_profile)
            except Exception:
                logger.exception("LLM call failed for this job")
        if not analysis_result:
            analysis_result = _fallback_analysis(has_llm_provider)

        # Scope filter: discard jobs whose central role is a different career
        # track than the candidate's (e.g. Android/QA/Excel/People Analytics).
        # Only an EXPLICIT false discards (see matcher._coerce_bool default).
        if not analysis_result.get("core_role_compatible", True):
            logger.info(
                "Discarded as out of scope (core role mismatch): "
                f"{job['job_title']} | {job['company']} (match {analysis_result.get('match_score')})"
            )
            continue

        analyzed_job = {**job, **analysis_result}
        analyzed_jobs.append(analyzed_job)
        logger.info(f"Result: Score {analyzed_job['match_score']}/100")

    return analyzed_jobs


def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("LINKEDIN JOB SEARCH AND MATCH PIPELINE")
    logger.info("=" * 60)

    # 1. Load environment variables (.env)
    load_dotenv()

    # Check available LLM providers (OpenRouter primary, Gemini fallback).
    has_llm_provider = has_provider()
    if not has_llm_provider:
        logger.warning("No LLM provider configured (OPENROUTER_API_KEY/GEMINI_API_KEY) in the .env file.")
        logger.warning("The script will continue with the job search, but the match step will use a default (simulated) score.")

    # 2. Load settings from the YAML file
    config_path = env_str("KEYWORDS_CONFIG_PATH", "config/keywords.yaml")
    if not os.path.exists(config_path):
        logger.error(f"Configuration file '{config_path}' not found!")
        sys.exit(1)

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load the YAML file '{config_path}': {e}")
        sys.exit(1)

    # NOTE: config keys (termos_busca, localizacao, filtros_busca, ...) come from
    # keywords.yaml and are kept in Portuguese on purpose.
    search_terms = config.get("termos_busca", [])
    location = config.get("localizacao", "Brasil")
    contract_type = config.get("tipo_contratacao", "CLT")
    posting_period = config.get("tempo_publicacao")
    max_jobs_per_term = int(config.get("max_vagas_por_termo", 3))
    search_filters = config.get("filtros_busca") or [{"modelo_trabalho": None, "localizacao": location}]
    candidate_profile = format_candidate_profile(config.get("perfil_candidato"))

    if not search_terms:
        logger.error("Empty 'termos_busca' list in the configuration file.")
        sys.exit(1)

    if not candidate_profile:
        logger.error("'perfil_candidato' not configured or empty in the configuration file.")
        sys.exit(1)

    logger.info(f"Search terms found: {search_terms}")
    logger.info(f"Contract type: {contract_type}")
    logger.info(f"Posting time filter: {posting_period or 'no filter'}")
    logger.info(f"Search filters: {search_filters}")
    logger.info(f"Candidate profile loaded ({len(candidate_profile)} characters).")

    contract_classifier = None
    if str(contract_type).strip().lower() == "clt":
        if not has_llm_provider:
            logger.error(
                "CLT contract filtering uses the LLM (same OpenRouter/Gemini chain "
                "as the match). Configure OPENROUTER_API_KEY and/or GEMINI_API_KEY in .env."
            )
            sys.exit(1)
        contract_classifier = LLMContractClassifier()
        logger.info(
            "LLM contract classification enabled (default-CLT: only discards "
            f"non-CLT types with confidence >= {min_discard_confidence():.2f})."
        )

    history_path = env_str("HISTORY_PATH", "vagas_historico.json")
    job_history = load_job_history(history_path)
    history_links = set(job_history.keys())
    logger.info(f"Known jobs loaded from history: {len(history_links)}")

    # 3. Run the scraper to collect LinkedIn jobs.
    # max_jobs_per_term (config: max_vagas_por_termo) caps how many jobs we pull
    # per search term to stay quick and avoid blocks; raise it in the YAML.
    collected_jobs = []
    collected_links = set()

    for term in search_terms:
        for search_filter in search_filters:
            filter_location = search_filter.get("localizacao", location)
            workplace_type = search_filter.get("modelo_trabalho")
            filter_geo_id = search_filter.get("geo_id")
            try:
                jobs = scrape_linkedin_jobs(
                    term,
                    location=filter_location,
                    max_jobs=max_jobs_per_term,
                    contract_type=contract_type,
                    workplace_type=workplace_type,
                    excluded_links=history_links | collected_links,
                    contract_classifier=contract_classifier,
                    geo_id=filter_geo_id,
                    time_filter=posting_period,
                )
                for job in jobs:
                    job_link = job.get("job_link")
                    if job_link in collected_links:
                        logger.info(f"Duplicate job ignored: {job.get('job_title')} | {job.get('company')}")
                        continue
                    collected_links.add(job_link)
                    collected_jobs.append(job)
            except Exception:
                logger.exception(f"Error searching jobs for term '{term}' with filter {search_filter}")
                continue

    total_collected = len(collected_jobs)
    logger.info(f"Search finished! Total jobs collected across all searches: {total_collected}")

    # Dedup before the LLM: collapses reposts of the same job (same
    # title+company) that came with different IDs, saving LLM calls.
    collected_jobs = dedupe_jobs(collected_jobs)
    total_jobs = len(collected_jobs)
    removed = total_collected - total_jobs
    if removed:
        logger.info(f"Dedup: {removed} near-duplicate(s) removed. Unique jobs to analyze: {total_jobs}")

    if total_jobs == 0:
        logger.info("No new job could be collected. Ending pipeline.")
        sys.exit(0)

    # 4. Run the matcher to analyze each collected job (and drop out-of-scope ones)
    logger.info("=" * 40)
    logger.info("STARTING MATCH ANALYSIS")
    logger.info("=" * 40)

    analyzed_jobs = analyze_and_filter_jobs(collected_jobs, candidate_profile, has_llm_provider)

    out_of_scope = total_jobs - len(analyzed_jobs)
    if out_of_scope:
        logger.info(f"Scope filter: {out_of_scope} of {total_jobs} job(s) discarded as out of scope (not persisted).")

    if not analyzed_jobs:
        logger.info("No in-scope job left after analysis. Ending pipeline.")
        sys.exit(0)

    # Relevance gate BEFORE persisting/reporting: only surface jobs that are
    # confidently CLT (score_clt >= 0.7, no "N/A") AND well matched
    # (match_score >= 70). Everything below the bar is dropped here so it never
    # reaches the history DB or the web UI ("não quero nem que sejam
    # selecionados scores inferiores a 0,7"). Persistent contract retry (see
    # matcher.contract_max_cycles) keeps "N/A" from occurring in the first place.
    relevant_jobs = [j for j in analyzed_jobs if passes_relevance_filter(j)]
    dropped = len(analyzed_jobs) - len(relevant_jobs)
    if dropped:
        logger.info(
            f"Relevance gate: {dropped} of {len(analyzed_jobs)} job(s) below the bar "
            f"(CLT >= {min_clt_score():.2f} and match >= {min_match_score():.0f}) — not persisted."
        )
    if not relevant_jobs:
        logger.info("No job cleared the relevance bar. Ending pipeline.")
        sys.exit(0)

    # 5. Run the reporter to generate the output Markdown file
    output_path = env_str("REPORT_OUTPUT_PATH", "vagas_filtradas.md")
    report_saved = generate_report(relevant_jobs, output_path=output_path)
    history_saved = save_job_history(history_path, job_history, relevant_jobs)

    if report_saved and history_saved:
        logger.info("=" * 60)
        logger.info("PIPELINE COMPLETED SUCCESSFULLY!")
        logger.info("The report 'vagas_filtradas.md' is available at the project root.")
        logger.info("The history 'vagas_historico.json' has been updated.")
        logger.info("=" * 60)
    else:
        logger.error("Failed to generate the final report or update the history.")
        sys.exit(1)

if __name__ == "__main__":
    main()

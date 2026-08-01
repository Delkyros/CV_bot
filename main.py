import os
import re
import sys
import json
import logging
import collections
from datetime import datetime
import yaml
from dotenv import load_dotenv

from src.scraper import scrape_linkedin_jobs, normalize_text
from src.text_signals import out_of_scope_title, title_head
from src.matcher import analyze_match, has_provider
from src.local_match import local_match_analysis, description_similarity, title_scope_similarity
from src.run_controller import emit_progress
from src.reporter import passes_relevance_filter, min_clt_score, min_match_score
from src.logging_config import setup_logging
from src.settings import env_str, env_float, env_int, env_bool
from src.match_labels import label_of

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


# English headers for the profile keys the matcher prompt has always used. Any
# OTHER key falls back to _prettify_label, so `perfil_candidato` can grow in the
# YAML (experiencia, formacao, idiomas, preferencias, ...) with no code change.
_PROFILE_LABELS = {
    "resumo_profissional": "Professional Summary",
    "competencias_tecnicas": "Technical Skills (Hard Skills)",
    "soft_skills": "Soft Skills",
    "nivel_experiencia": "Experience Level",
}


def _inline(value):
    """One-line form of a YAML value, for use inside a bullet. Nested dicts/lists
    are flattened (a list of dicts — e.g. experiencia entries — reads as
    'Cargo: X | Empresa: Y')."""
    if isinstance(value, dict):
        return " | ".join(f"{_prettify_label(k)}: {_inline(v)}" for k, v in value.items() if v)
    if isinstance(value, (list, tuple)):
        return ", ".join(_inline(v) for v in value if v)
    return str(value).strip()


def format_candidate_profile(profile):
    """
    Convert the candidate profile (structured dict from the YAML, or plain text
    for backward compatibility) into a readable running text for the matcher
    prompt. The LLM receives formatted text, not the repr of a dict.

    Every key present is rendered, in YAML order — there is no allowlist, so
    adding depth to `perfil_candidato` needs no change here. Scalars become
    "Label: value", lists a bullet each, dicts one "- SubLabel: values" line per
    entry. Empty values are skipped.

    NOTE: the profile dict keys come from config/keywords.yaml and are kept in
    Portuguese on purpose (that file is not translated); only the four historical
    keys get an English header (_PROFILE_LABELS).
    """
    if profile is None:
        return ""
    if not isinstance(profile, dict):
        return str(profile).strip()

    sections = []
    for key, value in profile.items():
        if not value:
            continue
        label = _PROFILE_LABELS.get(key, _prettify_label(key))
        if isinstance(value, dict):
            lines = [f"- {_prettify_label(k)}: {_inline(v)}" for k, v in value.items() if v]
            sections.append(f"{label}:\n" + "\n".join(lines))
        elif isinstance(value, (list, tuple)):
            lines = [f"- {_inline(v)}" for v in value if v]
            sections.append(f"{label}:\n" + "\n".join(lines))
        else:
            text = str(value).strip()
            # Multi-line scalars (YAML `|` blocks) keep the header on its own line.
            sections.append(f"{label}:\n{text}" if "\n" in text else f"{label}: {text}")

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


def _title_company_key(title, company):
    return (normalize_text(title or ""), normalize_text(company or ""))


def _is_untriaged(entry):
    return entry.get("status") in (None, "new")


def triaged_title_company_keys(history):
    """(title, company) keys of history entries the user already dealt with
    (any status other than 'new'). Reposts of these — same ad, new LinkedIn
    ID/link — are skipped at collection time so they never re-enter triage."""
    return {
        _title_company_key(entry.get("job_title"), entry.get("company"))
        for entry in history.values()
        if not _is_untriaged(entry)
    }


def save_job_history(history_path, history, analyzed_jobs):
    now = datetime.now().isoformat(timespec="seconds")

    # Reload from disk so any status/notes set via the web UI *during* this run
    # (which started minutes ago with a now-stale in-memory copy) are not lost.
    # Disk is authoritative for existing entries; fall back to the in-memory
    # copy only if the file could not be read.
    merged = load_job_history(history_path) or dict(history)

    # Untriaged entries indexed by (title, company): a freshly scraped repost
    # supersedes them — the new link is the active ad the user should triage,
    # the stale one becomes status="duplicado" (kept in the history so its URL
    # stays excluded from future scrapes). Entries with a user status are
    # never touched; built from the disk reload above so a mark set mid-run wins.
    untriaged_by_key = {}
    for old_link, old_entry in merged.items():
        if _is_untriaged(old_entry):
            key = _title_company_key(old_entry.get("job_title"), old_entry.get("company"))
            untriaged_by_key.setdefault(key, []).append(old_link)

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
            # Work-model diagnostics (src/scraper.py): `location_shape` is the
            # ranking signal for the f_WT remote leak, `workplace_evidence` records
            # whether an on-site signal was present but vetoed by a remote word.
            "location_shape": job.get("location_shape"),
            "workplace_evidence": job.get("workplace_evidence"),
            "match_score": job.get("match_score", 0),
            "score_gemini": job.get("score_gemini"),
            "score_vetor_desc": job.get("score_vetor_desc"),
            "score_title": job.get("score_title"),
            # Analysis prose for the web-app "Relatório" tab (was the .md report).
            "strengths": job.get("strengths", []),
            "gaps": job.get("gaps", []),
            "verdict": job.get("verdict", ""),
            "first_seen_at": prior.get("first_seen_at", now),
            "last_processed_at": now,
        }
        # Preserve user-set status/notes from the web UI.
        for field in USER_STATUS_FIELDS:
            if field in prior:
                entry[field] = prior[field]

        # First time we see a sub-bar job (no user status yet), flag it
        # "irrelevant" so the web UI keeps it out of the "new" list. A user
        # status (viewed/applied/error/new) always wins and is never overwritten.
        if "status" not in prior and not passes_relevance_filter(job):
            entry["status"] = "irrelevant"

        merged[link] = entry

        # Repost supersedes: older still-untriaged records of the same ad
        # leave the triage queue as "duplicado".
        key = _title_company_key(entry["job_title"], entry["company"])
        for old_link in untriaged_by_key.get(key, ()):
            if old_link != link:
                merged[old_link]["status"] = "duplicado"
                merged[old_link]["status_updated_at"] = now

    try:
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        logger.info(f"History updated at '{history_path}' ({len(merged)} known jobs).")
        return True
    except Exception as e:
        logger.error(f"Failed to save history '{history_path}': {e}")
        return False


def _fallback_analysis(has_llm_provider, job, candidate_profile):
    """LLM-free analysis so the pipeline never breaks when the LLM is unavailable
    or fails. Scores the job locally by embedding proximity (src/local_match.py);
    only if even that fails do we fall back to a neutral fixed score.
    core_role_compatible stays True: we never discard a job on infra failure."""
    reason = "no LLM provider configured" if not has_llm_provider else "all LLM providers failed"
    try:
        result = local_match_analysis(job, candidate_profile)
        logger.warning(f"Local embedding fallback ({reason}): score {result['match_score']}/100.")
        return result
    except Exception:
        logger.exception(f"Local embedding fallback failed ({reason}); using neutral score.")
        return {
            "match_score": 50,  # Neutral score
            "core_role_compatible": True,
            "strengths": ["Job collected successfully", "Available for evaluation"],
            "gaps": ["LLM and local analysis unavailable"],
            "verdict": "Nenhum provedor LLM disponível e o fallback local falhou; revise manualmente.",
        }


# Web-UI classification label (webapp.ERROR_CLASSES) for jobs the user marks as
# out-of-scope. Kept verbatim so the learned blocklist below reads the same string
# the UI persists; must match webapp.py.
SCOPE_ERROR_CLASS = "Escopo incorreto"
LOCATION_ERROR_CLASS = "Localidade/Modelo incorreto"

# Statuses that mean the user found the job worth their time. A company with any of
# these is never blocklisted, however many location errors it also produced.
ENGAGED_STATUSES = ("viewed", "applied")

# Minimum "Localidade/Modelo incorreto" marks before a company is blocked. 2 is
# deliberate, not tunable-by-taste: measured over the history, 80% of the companies
# behind those errors produced exactly ONE, so a threshold of 1 cannot generalize
# (the error IS the first sighting) while adding 112 companies of blast radius.
DEFAULT_LOCATION_BLOCKLIST_MIN_ERRORS = 2


def location_blocklist_min_errors():
    return env_int("LOCATION_BLOCKLIST_MIN_ERRORS", DEFAULT_LOCATION_BLOCKLIST_MIN_ERRORS)


def learned_location_blocklist(history):
    """Normalized company names to skip on sight: companies you flagged
    "Localidade/Modelo incorreto" at least LOCATION_BLOCKLIST_MIN_ERRORS times and
    NEVER viewed or applied to.

    The narrowness is the point. LinkedIn's f_WT=2 filter intermittently returns
    non-remote ads and exposes no per-job work-model field to catch them with
    (verified: neither the search card, the jobPosting criteria list, nor the public
    job page carries it), so the only remaining signals are historical. Measured on
    the user's own marks this blocks 16 companies and catches 30 of the 203 residual
    errors with ZERO collateral on jobs they engaged with. The engagement veto is
    what buys the zero -- without it the same rule costs 142 good jobs.
    """
    errors = collections.Counter()
    engaged = set()
    for entry in history.values():
        if not isinstance(entry, dict):
            continue
        company = normalize_text(entry.get("company") or "")
        if not company:
            continue
        if entry.get("error_class") == LOCATION_ERROR_CLASS:
            errors[company] += 1
        if entry.get("status") in ENGAGED_STATUSES:
            engaged.add(company)
    threshold = location_blocklist_min_errors()
    return {c for c, n in errors.items() if n >= threshold and c not in engaged}

# Embedding scope gate: a job is dropped only when BOTH signals are weak — the
# title is far from every role you search (score_title) AND the description is
# far from your CV (score_vetor_desc). Default-in-scope: a good title rescues a
# so-so description and vice-versa. Measured on the user's labeled history, the
# title signal alone at 0.40 keeps 92% of applied jobs while dropping 79% of
# 'Escopo incorreto' ones; the AND with the description makes it more permissive
# (safer). Both raw cosines 0..1; tune via SCOPE_TITLE_MIN / SCOPE_DESC_MIN.
DEFAULT_SCOPE_TITLE_MIN = 0.40
DEFAULT_SCOPE_DESC_MIN = 0.40


def scope_title_min():
    return env_float("SCOPE_TITLE_MIN", DEFAULT_SCOPE_TITLE_MIN)


def scope_desc_min():
    return env_float("SCOPE_DESC_MIN", DEFAULT_SCOPE_DESC_MIN)


def _scope_title_key(title):
    """Normalized title key for the learned scope blocklist: role portion only
    (see text_signals.title_head for the LinkedIn title noise it strips),
    accent-stripped and whitespace-collapsed, so a repost of the same role under a
    new link/city still matches."""
    return " ".join(normalize_text(title_head(title)).split())


def learned_scope_blocklist(history):
    """Titles the user marked 'Escopo incorreto' in the web UI, as normalized keys.

    This is the auto-adjusting scope filter: your own corrections feed straight
    back into the deterministic gate, so a role you rejected once is dropped on
    sight next time — no LLM call, no hand-written regex. Grows every time you
    triage. Complements the curated out_of_scope_title() patterns.
    """
    keys = set()
    for entry in history.values():
        if isinstance(entry, dict) and entry.get("error_class") == SCOPE_ERROR_CLASS:
            key = _scope_title_key(entry.get("job_title"))
            if key:
                keys.add(key)
    return keys


def build_scope_patterns(config):
    """Compile the title-scope SEED from config['escopo_fora_de_alvo'] into a list
    of (motivo, compiled_regex) pairs for out_of_scope_title().

    This is the config-driven half of the scope blocklist (the learned marks from
    learned_scope_blocklist are the other half — the two are unioned at the two
    gates in analyze_and_filter_jobs). An entry with an uncompilable `padrao` is
    skipped with a warning so one bad regex never aborts the run (Principle VI);
    a missing/empty key yields no seed patterns (the learned gate + embedding + LLM
    still filter — default-in-scope, Principle II).
    """
    entries = config.get("escopo_fora_de_alvo") or []
    patterns = []
    seen = set()  # dedupe identical padroes within the seed
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        motivo = entry.get("motivo") or "escopo fora de alvo"
        padrao = entry.get("padrao")
        if not padrao or padrao in seen:
            continue
        try:
            patterns.append((motivo, re.compile(padrao)))
            seen.add(padrao)
        except re.error as exc:
            logger.warning("Skipping invalid escopo_fora_de_alvo padrao %r (%s): %s", padrao, motivo, exc)
    return patterns


# ---------------------------------------------------------------------------
# Few-shot from the user's own decisions (spec 002, User Story 2). Built ONCE
# per run from the history the user already triaged, injected into the LLM match
# prompt so match_score aligns with their taste. Best-effort: any failure or
# too-few labels returns None → the exemplar-free prompt (Principle VI). It never
# changes a drop/keep decision — only the LLM score.
# ---------------------------------------------------------------------------

def _recent(entries, n):
    """The n most-recent entries by first_seen_at."""
    return sorted(entries, key=lambda e: e.get("first_seen_at", ""), reverse=True)[:n]


def _exemplar_line(entry):
    """One bullet from stored fields (no raw description is kept in history)."""
    title = entry.get("job_title", "N/A")
    company = entry.get("company", "N/A")
    why = entry.get("verdict") or (entry.get("gaps") or [""])[0] or ""
    why = " ".join(str(why).split())  # collapse whitespace
    if len(why) > 160:
        why = why[:157] + "…"
    tail = f" — {why}" if why else ""
    return f"- {title} @ {company}{tail}"


def build_fewshot_block(history):
    """Render the accept/reject exemplar block, or None to fall back to the
    exemplar-free prompt (disabled, too few labels, or any error)."""
    if not env_bool("FEWSHOT_ENABLED", True):
        return None
    try:
        pos = [e for e in history.values() if label_of(e) == "pos"]
        neg = [e for e in history.values() if label_of(e) == "neg"]
        if len(pos) + len(neg) < env_int("FEWSHOT_MIN_LABELS", 10):
            logger.info("Few-shot: not enough labeled jobs yet; using exemplar-free prompt.")
            return None
        max_ex = env_int("FEWSHOT_MAX_EXEMPLARS", 3)
        budget = env_int("FEWSHOT_CHAR_BUDGET", 1500)
        lines = [
            "The candidate has personally reviewed similar postings. Use these as calibration "
            "for their taste (do NOT copy scores; judge THIS posting on its merits):",
            "Examples the candidate CHOSE TO APPLY to:",
        ]
        lines += [_exemplar_line(e) for e in _recent(pos, max_ex)]
        lines.append("Examples the candidate REJECTED as out of scope / irrelevant:")
        lines += [_exemplar_line(e) for e in _recent(neg, max_ex)]
        # Truncate to the char budget line-by-line so the block never blows up.
        block, used = [], 0
        for line in lines:
            if used + len(line) + 1 > budget:
                break
            block.append(line)
            used += len(line) + 1
        if len(block) <= 3:  # header(s) only, no actual exemplars survived
            return None
        logger.info(f"Few-shot: injecting {len(block) - 3} exemplar(s) into the match prompt.")
        return "\n".join(block)
    except Exception:
        logger.exception("Few-shot exemplar selection failed; using exemplar-free prompt.")
        return None


def analyze_and_filter_jobs(collected_jobs, candidate_profile, has_llm_provider, scope_blocklist=None, search_terms=None, exemplars=None, scope_patterns=None):
    """Run the match analysis on each collected job and DROP the ones whose core
    role does not match the candidate's area (scope filter), so they never reach
    the report or the history DB.

    `scope_blocklist` is the learned set of normalized titles (learned_scope_blocklist)
    the user already rejected as out-of-scope — dropped deterministically here.
    `scope_patterns` is the config-driven title SEED (build_scope_patterns); the two
    together are the deduplicated union of the deterministic title scope filter.

    Returns the list of in-scope analyzed jobs (job data merged with the analysis).
    """
    scope_blocklist = scope_blocklist or set()
    scope_patterns = scope_patterns or []
    analyzed_jobs = []
    total_jobs = len(collected_jobs)

    step = max(1, total_jobs // 20)
    for idx, job in enumerate(collected_jobs, start=1):
        logger.info(f"Analyzing job {idx} of {total_jobs}: {job['job_title']} | Company: {job['company']}")
        if idx == 1 or idx == total_jobs or idx % step == 0:
            emit_progress("matching", done=idx, total=total_jobs, detail=f"analisando {idx}/{total_jobs}")

        # Deterministic title-based scope gate: the job TITLE is the most
        # reliable scope signal, so drop clearly out-of-track roles (BI/Qlik
        # dev, market intelligence/research, systems analyst, generic Node/
        # graduate dev) BEFORE spending an LLM call. Ambiguous titles fall
        # through to the LLM's core_role_compatible judgment below.
        out_reason = out_of_scope_title(job.get("job_title"), scope_patterns)
        if out_reason:
            logger.info(
                "Discarded as out of scope by title "
                f"({out_reason}): {job['job_title']} | {job['company']}"
            )
            continue

        # Learned gate: a title you already flagged 'Escopo incorreto' is dropped
        # deterministically, before the (unreliable, free) LLM can rubber-stamp it.
        if _scope_title_key(job.get("job_title")) in scope_blocklist:
            logger.info(
                "Discarded as out of scope (learned from your 'Escopo incorreto' marks): "
                f"{job['job_title']} | {job['company']}"
            )
            continue

        # Embedding scope gate: title vs the roles you search (score_title) and
        # description vs your CV (score_vetor_desc). Drop only when BOTH are weak
        # (default-in-scope: a good title rescues a so-so description and vice-
        # versa). Runs BEFORE the LLM so off-scope jobs cost no Gemini call.
        # Skipped when no search terms are given (e.g. unit tests) — scope is
        # undecidable then, so keep.
        title_sim = title_scope_similarity(job.get("job_title"), search_terms or [])
        desc_sim = description_similarity(job, candidate_profile)
        if search_terms and title_sim < scope_title_min() and desc_sim < scope_desc_min():
            logger.info(
                f"Discarded as out of scope (título~{title_sim:.2f} & desc~{desc_sim:.2f} "
                f"< {scope_title_min():.2f}/{scope_desc_min():.2f}): "
                f"{job['job_title']} | {job['company']}"
            )
            continue

        llm_result = None
        if has_llm_provider:
            try:
                llm_result = analyze_match(job, candidate_profile, exemplars)
            except Exception:
                logger.exception("LLM call failed for this job")
        analysis_result = llm_result or _fallback_analysis(has_llm_provider, job, candidate_profile)

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
        # Three scores side by side to compare whether the LLM is worth it:
        #   score_gemini      — LLM match (None when Gemini didn't answer)
        #   score_vetor_desc  — description × CV, raw cosine 0..1
        #   score_title       — title × termos_busca, raw cosine 0..1 (scope signal)
        analyzed_job["score_gemini"] = llm_result["match_score"] if llm_result else None
        analyzed_job["score_vetor_desc"] = round(desc_sim, 3)
        analyzed_job["score_title"] = round(title_sim, 3)
        analyzed_jobs.append(analyzed_job)
        logger.info(
            f"Result: gemini={analyzed_job['score_gemini']} "
            f"vetor_desc={analyzed_job['score_vetor_desc']} title={analyzed_job['score_title']}"
        )

    return analyzed_jobs


def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("LINKEDIN JOB SEARCH AND MATCH PIPELINE")
    logger.info("=" * 60)

    # 1. Load environment variables (.env)
    load_dotenv()

    # Check the LLM provider (Gemini; see src/matcher.py).
    has_llm_provider = has_provider()
    if not has_llm_provider:
        logger.warning("No LLM provider configured (GEMINI_API_KEY) in the .env file.")
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

    if str(contract_type).strip().lower() == "clt":
        logger.info(
            "Local contract classification enabled (default-CLT: discards only on "
            "explicit non-CLT signals — contractor/PJ/USD-hourly or internship)."
        )

    history_path = env_str("HISTORY_PATH", "vagas_historico.json")
    job_history = load_job_history(history_path)
    history_links = set(job_history.keys())
    logger.info(f"Known jobs loaded from history: {len(history_links)}")

    # LinkedIn reposts the same ad under new IDs, so the link check alone lets
    # already-triaged jobs back in. Skip reposts of triaged jobs at collection.
    triaged_keys = triaged_title_company_keys(job_history)
    logger.info(f"Triaged (title, company) pairs excluded from re-collection: {len(triaged_keys)}")

    # Auto-adjusting scope filter: titles you rejected as 'Escopo incorreto' in
    # the web UI become a deterministic blocklist for this run.
    scope_blocklist = learned_scope_blocklist(job_history)
    if scope_blocklist:
        logger.info(f"Learned scope blocklist: {len(scope_blocklist)} title(s) from your 'Escopo incorreto' marks.")

    # Same learn-from-your-marks idea, applied to the work-model leak: companies you
    # repeatedly flagged "Localidade/Modelo incorreto" and never engaged with.
    location_blocklist = learned_location_blocklist(job_history)
    if location_blocklist:
        logger.info(
            f"Learned location blocklist: {len(location_blocklist)} company(ies) with "
            f">={location_blocklist_min_errors()} 'Localidade/Modelo incorreto' marks and no viewed/applied job."
        )

    # Config-driven title scope SEED (escopo_fora_de_alvo), unioned with the
    # learned blocklist above at the two deterministic gates in the matcher.
    scope_patterns = build_scope_patterns(config)
    logger.info(f"Title scope seed patterns loaded: {len(scope_patterns)}.")

    # Few-shot exemplars from your own accept/reject marks (once per run).
    fewshot_exemplars = build_fewshot_block(job_history)

    # 3. Run the scraper to collect LinkedIn jobs.
    # max_jobs_per_term (config: max_vagas_por_termo) caps how many jobs we pull
    # per search term to stay quick and avoid blocks; raise it in the YAML.
    emit_progress("scraping", detail="coletando vagas no LinkedIn")
    collected_jobs = []
    collected_links = set()

    for term in search_terms:
        for search_filter in search_filters:
            filter_location = search_filter.get("localizacao", location)
            workplace_type = search_filter.get("modelo_trabalho")
            filter_geo_id = search_filter.get("geo_id")
            # Search radius in miles around geo_id (LinkedIn's `distance`). Only
            # meaningful for a CITY geoId (hybrid/on-site filters); omitted when
            # absent so LinkedIn's default applies.
            filter_distance = search_filter.get("distancia")
            try:
                jobs = scrape_linkedin_jobs(
                    term,
                    location=filter_location,
                    max_jobs=max_jobs_per_term,
                    contract_type=contract_type,
                    workplace_type=workplace_type,
                    excluded_links=history_links | collected_links,
                    excluded_title_companies=triaged_keys,
                    excluded_companies=location_blocklist,
                    geo_id=filter_geo_id,
                    time_filter=posting_period,
                    distance=filter_distance,
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

    emit_progress("dedupe", detail=f"{total_jobs} vaga(s) única(s) para analisar")

    if total_jobs == 0:
        logger.info("No new job could be collected. Ending pipeline.")
        emit_progress("done", detail="nenhuma vaga nova")
        sys.exit(0)

    # 4. Run the matcher to analyze each collected job (and drop out-of-scope ones)
    logger.info("=" * 40)
    logger.info("STARTING MATCH ANALYSIS")
    logger.info("=" * 40)

    analyzed_jobs = analyze_and_filter_jobs(collected_jobs, candidate_profile, has_llm_provider, scope_blocklist, search_terms, fewshot_exemplars, scope_patterns)

    out_of_scope = total_jobs - len(analyzed_jobs)
    if out_of_scope:
        logger.info(f"Scope filter: {out_of_scope} of {total_jobs} job(s) discarded as out of scope (not persisted).")

    if not analyzed_jobs:
        logger.info("No in-scope job left after analysis. Ending pipeline.")
        emit_progress("done", detail="nenhuma vaga no escopo")
        sys.exit(0)

    # Relevance split: jobs that are confidently CLT (score_clt >= 0.7, no "N/A")
    # AND well matched (match_score >= 70) are the ones worth surfacing. Sub-bar
    # jobs are still PERSISTED (so they are not re-scraped/re-classified every
    # run) but flagged status="irrelevant" so they never clutter the "new" list
    # in the web UI. Persistent contract retry keeps "N/A" from happening.
    relevant_jobs = [j for j in analyzed_jobs if passes_relevance_filter(j)]
    irrelevant = len(analyzed_jobs) - len(relevant_jobs)
    if irrelevant:
        logger.info(
            f"Relevance split: {irrelevant} of {len(analyzed_jobs)} job(s) below the bar "
            f"(CLT >= {min_clt_score():.2f} and match >= {min_match_score():.0f}) — "
            "persisted as 'irrelevant', hidden from the new list."
        )

    # 5. Persist history (the web-app "Relatório" tab reads it — the old .md
    # report was dropped). The history keeps ALL in-scope jobs (sub-bar ones
    # flagged 'irrelevant'); relevant_jobs above is only for the split logging.
    emit_progress("reporting", detail="salvando histórico")
    history_saved = save_job_history(history_path, job_history, analyzed_jobs)
    emit_progress("done", detail="concluído")

    if history_saved:
        logger.info("=" * 60)
        logger.info("PIPELINE COMPLETED SUCCESSFULLY!")
        logger.info("The history 'vagas_historico.json' has been updated.")
        logger.info("=" * 60)
    else:
        logger.error("Failed to update the history.")
        sys.exit(1)

if __name__ == "__main__":
    main()

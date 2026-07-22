import os
import json
import re
import time
import logging

from google import genai
from google.genai import types

from src.text_signals import internship_evidence, strong_non_clt_evidence
from src.settings import env_float, env_int, env_str

logger = logging.getLogger(__name__)

# Defaults for tunables. Every value here is overridable via the environment
# (.env); see env_*() reads below and .env.example for the variable names.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Gemini is the only LLM provider. OpenRouter (free pool) was removed after the
# score comparison: unreliable AND its scores were garbage (84% of user-rejected
# jobs got 100). It lives in git history if it's ever wanted back.

# How many times to retry the Gemini call before giving up and falling back to
# main's local analysis. Between attempts, wait LLM_QUOTA_RETRY_WAIT seconds.
# Kept low on purpose: failing fast to the local fallback (embedding match
# score) is both faster and lossless.
DEFAULT_MAX_PROVIDER_CYCLES = 2
DEFAULT_QUOTA_RETRY_WAIT = 5.0

# Sampling/budget defaults for the LLM calls.
DEFAULT_LLM_TEMPERATURE = 0.1
DEFAULT_LLM_REQUEST_TIMEOUT = 60


def gemini_model():
    return env_str("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)


def max_provider_cycles():
    return env_int("LLM_MAX_PROVIDER_CYCLES", DEFAULT_MAX_PROVIDER_CYCLES)


def quota_retry_wait():
    return env_float("LLM_QUOTA_RETRY_WAIT", DEFAULT_QUOTA_RETRY_WAIT)


def llm_temperature():
    return env_float("LLM_TEMPERATURE", DEFAULT_LLM_TEMPERATURE)


def llm_request_timeout():
    return env_int("LLM_REQUEST_TIMEOUT", DEFAULT_LLM_REQUEST_TIMEOUT)

# Keywords that indicate a transient error (quota/limit/overload). For those
# cases it is worth trying the next provider / repeating the cycle instead of
# failing.
_QUOTA_KEYWORDS = (
    "resource_exhausted", "resource exhausted", "quota", "rate limit",
    "rate-limit", "ratelimit", "too many requests", "too many sessions",
    "overloaded", "unavailable", "try again later",
    "http 429", "http 500", "http 502", "http 503", "http 529",
)

_client = None


def _get_client():
    """Create the Google GenAI client once (reused across jobs)."""
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


def _gemini_key():
    key = os.getenv("GEMINI_API_KEY")
    if key and key != "sua_chave_do_gemini_aqui":
        return key
    return None


def _is_retryable_quota_error(exc):
    """True for transient quota/limit/overload errors (any provider)."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 529):
        return True
    text = str(exc).lower()
    return any(keyword in text for keyword in _QUOTA_KEYWORDS)


def _build_prompt(job_info, candidate_profile, exemplars=None):
    """Build the match prompt. `exemplars`, when a non-empty string, is a
    pre-rendered block of the candidate's own past accept/reject decisions
    (prepared by main.py) inserted before the analysis instructions to calibrate
    the LLM to the user's taste. When falsy the prompt is byte-identical to the
    exemplar-free version (verified by tests) — few-shot never changes the
    response contract or any drop/keep decision."""
    prompt = f"""
You are a senior Technology Recruiting and Selection specialist (Tech Recruiter).
Your mission is to critically and realistically analyze whether a candidate is a good fit for a given job posting.
The job description and candidate profile may be written in Portuguese; analyze them as-is.

Job Data:
- Job Title: {job_info.get('job_title', 'N/A')}
- Company: {job_info.get('company', 'N/A')}
- Location: {job_info.get('location', 'N/A')}
- Job Description:
{job_info.get('full_description', 'No description.')}

Candidate Professional Profile:
{candidate_profile}

---

Analysis Instructions:
1. Compare the job's technical requirements, experience level and competencies with the candidate's profile.
2. Determine a realistic match score from 0 to 100 (be realistic and critical; do not give 100 unless every requirement matches perfectly).
3. Decide whether the job's CORE ROLE is in the candidate's primary professional area as described in the profile. Anchor this decision on the JOB TITLE / central function, NOT on incidental tech-stack overlap in the description (a posting mentioning Python or SQL does not make a non-DS role compatible). Set "core_role_compatible" to false when the central function is clearly a different career track than the candidate's — for example: mobile/Android/iOS, embedded/firmware, dedicated QA/testing, front-end-only, Excel/data-entry, HR/People Analytics, business-process analysis, research in an unrelated domain, BI/dashboard development (e.g. Qlik, Power BI), market intelligence / market research, systems analysis ("analista de sistemas"), or generic/junior software development (e.g. Node.js/back-end/full-stack developer, graduate/trainee programmer) that is not data science / machine learning. If the role is in or adjacent to the candidate's area (data science, machine learning, AI, data engineering), or you are unsure, set it to true.
4. List up to 4 candidate strengths that directly match the job requirements.
5. List the "gaps", i.e. the job's required or desired requirements that the candidate lacks or did not mention in their profile.
6. Write a friendly, honest and direct verdict (at most 3 sentences) advising what to focus on or whether it is worth applying.
Write the strengths, gaps and verdict in Portuguese (the candidate's language).

You MUST respond strictly in the JSON format below, with no explanatory blocks or markdown outside the JSON.
Desired response structure:
{{
  "match_score": <integer from 0 to 100>,
  "core_role_compatible": <true or false>,
  "strengths": ["Strength 1", "Strength 2", ...],
  "gaps": ["Gap 1", "Gap 2", ...],
  "verdict": "<Verdict text>"
}}
"""
    if exemplars:
        # Insert the exemplar block right before the analysis instructions. The
        # replace is a no-op when `exemplars` is falsy, keeping the prompt
        # byte-identical to the pre-feature version.
        marker = "\n---\n\nAnalysis Instructions:"
        prompt = prompt.replace(marker, f"\n{exemplars}{marker}", 1)
    return prompt


def _call_gemini(prompt):
    """Call Gemini. Returns the raw response text or raises an exception."""
    client = _get_client()
    response = client.models.generate_content(
        model=gemini_model(),
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=llm_temperature(),
        ),
    )
    return response.text


def has_provider():
    """True if the LLM provider (Gemini) is configured."""
    return bool(_gemini_key())


def _parse_result(raw_text):
    """Convert the raw LLM text into the standardized dict, or None if invalid."""
    if not raw_text:
        return None

    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict):
        return None

    try:
        score = int(result.get("match_score", 0))
    except (ValueError, TypeError):
        score = 0

    strengths = result.get("strengths", [])
    gaps = result.get("gaps", [])
    verdict = result.get("verdict", "No verdict.")

    return {
        "match_score": max(0, min(100, score)),
        "core_role_compatible": _coerce_bool(result.get("core_role_compatible", True)),
        "strengths": [str(x) for x in strengths] if isinstance(strengths, list) else [],
        "gaps": [str(x) for x in gaps] if isinstance(gaps, list) else [],
        "verdict": str(verdict),
    }


def _coerce_bool(value, default=True):
    """Interpret an LLM-provided boolean that may arrive as a real bool or a
    string ("false"/"no"/"não"/"0"). Defaults to True (keep the job) so an
    omitted/ambiguous value never discards a job — scope filtering only acts on
    an EXPLICIT negative, mirroring the conservative default-CLT philosophy."""
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "nao", "não", "0", "")
    if value is None:
        return default
    return bool(value)


def _gemini_complete(prompt, parse_fn, task="analysis", max_cycles=None, retry_wait=None):
    """
    Call Gemini with `prompt`, validating the response with `parse_fn`. On a
    quota/limit error or invalid JSON, wait `retry_wait`s and retry, up to
    `max_cycles` attempts.

    `max_cycles`/`retry_wait` default to the env-configured values
    (LLM_MAX_PROVIDER_CYCLES / LLM_QUOTA_RETRY_WAIT) when not passed explicitly.

    Returns the result of parse_fn (first valid response) or None if no key is
    configured or all attempts failed.
    """
    if max_cycles is None:
        max_cycles = max_provider_cycles()
    if retry_wait is None:
        retry_wait = quota_retry_wait()

    if not has_provider():
        logger.error("No LLM provider configured (set GEMINI_API_KEY in .env).")
        return None

    for cycle in range(1, max_cycles + 1):
        raw = None
        try:
            raw = _call_gemini(prompt)
        except Exception as exc:
            level = "quota/limit" if _is_retryable_quota_error(exc) else "non-recoverable failure"
            msg = str(exc).replace("\n", " ")
            if len(msg) > 200:
                msg = msg[:200] + "…"
            logger.warning(f"[gemini] {level} on {task}: {msg}.")

        if raw is not None:
            result = parse_fn(raw)
            if result is not None:
                if cycle > 1:
                    logger.info(f"{task} obtained on retry (cycle {cycle}).")
                return result
            logger.warning(f"[gemini] returned invalid JSON on {task}.")

        if cycle < max_cycles:
            logger.warning(
                f"{task} failed (cycle {cycle}/{max_cycles}). "
                f"Waiting {retry_wait:.0f}s and retrying..."
            )
            if retry_wait > 0:
                time.sleep(retry_wait)

    logger.error(f"All Gemini attempts failed on {task} after the retry cycles.")
    return None


def analyze_match(job_info, candidate_profile, exemplars=None):
    """
    Compare a job against the candidate profile using Gemini.
    `exemplars` (optional) is a pre-rendered block of the user's own accept/reject
    decisions injected into the prompt to align match_score with their taste.
    Returns the dict {match_score, strengths, gaps, verdict} or None (so that main
    uses the local fallback analysis).
    """
    prompt = _build_prompt(job_info, candidate_profile, exemplars)
    return _gemini_complete(prompt, _parse_result, "Match analysis")


# ---------------------------------------------------------------------------
# Employment-type classification (CLT vs PJ/freelancer/internship) — LOCAL, no
# LLM. CLT vs non-CLT is an explicit-keyword problem, not a semantic one, so it
# is decided entirely by the high-precision regex signals in text_signals.py.
# This is also the biggest speed win: it removes a per-job LLM call (and its
# retry storm on the rate-limited free pool) from collection.
#
# Default-CLT policy (load-bearing): a job is discarded ONLY on explicit non-CLT
# evidence — contractor/PJ/USD-hourly cues (strong_non_clt_evidence) or an
# internship marker (internship_evidence). Everything else is kept as assumed
# CLT with a fixed high confidence so it clears the report's CLT bar.
# ---------------------------------------------------------------------------

# Synthetic CLT confidence for jobs kept by the default (no non-CLT signal).
# Must stay above REPORT_MIN_CLT_SCORE (0.7) or default-CLT jobs would be hidden
# from the report despite being kept in history.
_ASSUMED_CLT_CONFIDENCE = 0.9


def _discard_result(regime, evidence):
    return {
        "inferred_contract_type": regime,
        "accepted": False,
        "score_clt": "N/A",
        "score_non_clt": 1.0,
        "contract_margin": "N/A",
        "contract_evidence": evidence,
    }


def classify_contract(description, title=None, company=None):
    """
    Classify the employment type LOCALLY (no LLM) and return the standardized dict
    the scraper/reporter/history expect.

    Default-CLT: discard ONLY on explicit non-CLT evidence — strong contractor/PJ
    cues (USD-hourly pay, "Contract:" titles, 1099/C2C, PJ/CNPJ markers, a known
    contractor-marketplace company) or an internship marker. Anything else is
    accepted as assumed CLT.
    """
    # Contractor cues frequently live in the TITLE ("$45/hr", "Contract: …") and
    # the platform in the COMPANY name, so scan all three together.
    scan_text = " ".join(part for part in (title, company, description) if part)

    strong_signals = strong_non_clt_evidence(scan_text, company=company)
    if strong_signals:
        return _discard_result(
            "PJ", "Discarded by strong non-CLT signal(s): " + ", ".join(strong_signals)
        )

    intern_signals = internship_evidence(scan_text)
    if intern_signals:
        return _discard_result(
            "ESTAGIO", "Discarded by internship signal(s): " + ", ".join(intern_signals)
        )

    return {
        "inferred_contract_type": "CLT",
        "accepted": True,
        "score_clt": _ASSUMED_CLT_CONFIDENCE,
        "score_non_clt": "N/A",
        "contract_margin": "N/A",
        "contract_evidence": "No explicit non-CLT signal; assumed CLT (default policy).",
    }

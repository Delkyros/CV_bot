import os
import json
import re
import time
import logging

import requests
from google import genai
from google.genai import types

from src.text_signals import internship_evidence, strong_non_clt_evidence
from src.settings import env_bool, env_float, env_int, env_list, env_str

logger = logging.getLogger(__name__)

# Defaults for tunables. Every value here is overridable via the environment
# (.env); see env_*() reads below and .env.example for the variable names.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Primary provider: OpenRouter (free models). Gemini is the fallback.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Default list of free OpenRouter models, tried in order. The most popular ones
# (Llama 70B, Qwen) live in 429 ("rate-limited upstream") because the free
# pool is congested, so we prioritize strong but less contested models. On a
# 429 for one model, we move on to the next.
# Override the whole list via OPENROUTER_MODELS (comma-separated) or pin a
# single model via OPENROUTER_MODEL.
DEFAULT_OPENROUTER_MODELS = (
    "openai/gpt-oss-120b:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemini-2.0-flash-exp:free",
)
PROVIDER_ORDER = ("openrouter", "gemini")

# Public catalog of OpenRouter models (no API key needed). Used to auto-discover
# the currently-free models (pricing == 0) and append them to the fallback list,
# so the chain keeps working as free model IDs come and go. There is no official
# "free-only auto" model on OpenRouter (openrouter/auto can route to PAID models),
# so we filter by price ourselves.
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_AUTO_FREE_MODELS = True
# Cap the auto-discovered extras low: trying 25 free IDs per call meant many
# 404s (IDs delisted between the /models fetch and the call) and 429s, all of
# which cost a round-trip before the chain moved on. A handful of extras is
# enough insurance behind the curated list.
MAX_AUTO_FREE_MODELS = 6
_free_models_cache = None  # None = not fetched yet; list = fetched (maybe empty)

# How many times to walk the full provider chain before giving up and falling
# back to main's local analysis. Raise (LLM_MAX_PROVIDER_CYCLES) for an
# almost "infinite until processed" behavior. Between cycles, wait
# LLM_QUOTA_RETRY_WAIT seconds. Kept low: when the free pool is saturated,
# sleeping minutes per job (the old 3×60s) made a run take hours — and the
# wait cannot un-congest the pool anyway. Failing fast to the local fallback
# (embedding match score / default-CLT) is both faster and lossless.
DEFAULT_MAX_PROVIDER_CYCLES = 2
DEFAULT_QUOTA_RETRY_WAIT = 5.0

# Sampling/budget defaults for the LLM calls.
DEFAULT_LLM_TEMPERATURE = 0.1
DEFAULT_OPENROUTER_MAX_TOKENS = 2000
DEFAULT_LLM_REQUEST_TIMEOUT = 60


def gemini_model():
    return env_str("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)


def max_provider_cycles():
    return env_int("LLM_MAX_PROVIDER_CYCLES", DEFAULT_MAX_PROVIDER_CYCLES)


def quota_retry_wait():
    return env_float("LLM_QUOTA_RETRY_WAIT", DEFAULT_QUOTA_RETRY_WAIT)


def llm_temperature():
    return env_float("LLM_TEMPERATURE", DEFAULT_LLM_TEMPERATURE)


def openrouter_max_tokens():
    return env_int("OPENROUTER_MAX_TOKENS", DEFAULT_OPENROUTER_MAX_TOKENS)


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


def _openrouter_key():
    return os.getenv("OPENROUTER_API_KEY")


def _gemini_key():
    key = os.getenv("GEMINI_API_KEY")
    if key and key != "sua_chave_do_gemini_aqui":
        return key
    return None


def auto_free_models_enabled():
    return env_bool("OPENROUTER_AUTO_FREE_MODELS", DEFAULT_AUTO_FREE_MODELS)


def _is_zero_price(price):
    try:
        return float(price) == 0.0
    except (TypeError, ValueError):
        return False


def _outputs_text(model):
    """True if the model outputs TEXT ONLY (what we can consume).

    Requires text to be the sole output modality, so multi-modal generators that
    also emit audio/image (e.g. lyria) are excluded. Reads OpenRouter's
    architecture.output_modalities; defaults to True when the metadata is absent
    so we never drop a usable model over missing fields.
    """
    arch = model.get("architecture") or {}
    outputs = arch.get("output_modalities")
    if isinstance(outputs, list):
        return outputs == ["text"]
    modality = arch.get("modality")
    if isinstance(modality, str) and modality:
        return modality.endswith("->text")
    return True


def _fetch_free_openrouter_models():
    """Discover currently-free OpenRouter models (prompt and completion priced 0).

    Best-effort and cached for the process lifetime: returns [] on any failure so
    the curated list still drives the chain. The /models endpoint is public, so
    no API key is required.
    """
    global _free_models_cache
    if _free_models_cache is not None:
        return _free_models_cache

    free = []
    try:
        resp = requests.get(OPENROUTER_MODELS_URL, timeout=llm_request_timeout())
        if resp.status_code == 200:
            for model in resp.json().get("data", []):
                pricing = model.get("pricing") or {}
                model_id = model.get("id")
                if not (model_id and _is_zero_price(pricing.get("prompt")) and _is_zero_price(pricing.get("completion"))):
                    continue
                # Keep only text-output models: skips audio/image/safety models
                # (e.g. lyria, content-safety) that can't answer our JSON prompt.
                if not _outputs_text(model):
                    continue
                free.append(model_id)
            if free:
                logger.info(f"[openrouter] auto-discovered {len(free)} free model(s) from /models.")
        else:
            logger.warning(f"[openrouter] /models returned HTTP {resp.status_code}; using curated list only.")
    except requests.RequestException as exc:
        logger.warning(f"[openrouter] could not fetch the free model list: {exc}. Using curated list only.")

    _free_models_cache = free[:MAX_AUTO_FREE_MODELS]
    return _free_models_cache


def _openrouter_models():
    # Resolution order:
    #   1. OPENROUTER_MODEL  -> pin a single model (highest priority).
    #   2. OPENROUTER_MODELS -> comma-separated list, tried in order.
    #   3. DEFAULT_OPENROUTER_MODELS built-in list.
    # Then, unless disabled, append any currently-free models discovered from
    # OpenRouter (deduped, after the curated ones) as extra fallbacks.
    single = os.getenv("OPENROUTER_MODEL")
    if single and single.strip():
        return [single.strip()]

    models = env_list("OPENROUTER_MODELS", DEFAULT_OPENROUTER_MODELS)
    if auto_free_models_enabled():
        seen = set(models)
        for model_id in _fetch_free_openrouter_models():
            if model_id not in seen:
                models.append(model_id)
                seen.add(model_id)
    return models


def _is_retryable_quota_error(exc):
    """True for transient quota/limit/overload errors (any provider)."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 529):
        return True
    text = str(exc).lower()
    return any(keyword in text for keyword in _QUOTA_KEYWORDS)


def _build_prompt(job_info, candidate_profile):
    return f"""
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


def _call_openrouter(prompt):
    """Call OpenRouter (OpenAI-compatible API), trying each model in the list in
    order and skipping the congested ones (429). Returns the raw text of the
    first valid response or raises the last exception."""
    key = _openrouter_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY missing")

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last_error = RuntimeError("No OpenRouter model available")
    skipped = []  # (short_model, reason) for busy/failing models — summarized once

    for model in _openrouter_models():
        short = model.split("/")[-1]
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": llm_temperature(),
                    "response_format": {"type": "json_object"},
                    # Generous output budget: reasoning models (e.g. gpt-oss)
                    # spend tokens "thinking" and, without this, the JSON comes
                    # out truncated.
                    "max_tokens": openrouter_max_tokens(),
                    # Minimize reasoning to leave budget for the final JSON.
                    "reasoning": {"effort": "low"},
                },
                timeout=llm_request_timeout(),
            )
            if resp.status_code != 200:
                last_error = RuntimeError(f"OpenRouter HTTP {resp.status_code} ({model}): {resp.text[:200]}")
                skipped.append(f"{short}:{resp.status_code}")
                continue
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if skipped:
                logger.info(f"[openrouter] {short} OK after skipping {len(skipped)} busy model(s): {', '.join(skipped)}")
            return content
        except (KeyError, IndexError, TypeError) as exc:
            last_error = RuntimeError(f"Unexpected OpenRouter response ({model}): {exc}")
            skipped.append(f"{short}:bad-response")
            continue
        except requests.RequestException as exc:
            last_error = RuntimeError(f"OpenRouter network error ({model}): {exc}")
            skipped.append(f"{short}:network")
            continue

    if skipped:
        logger.warning(f"[openrouter] all {len(skipped)} model(s) failed: {', '.join(skipped)}")
    raise last_error


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


_PROVIDER_CALLS = {
    "openrouter": (_openrouter_key, _call_openrouter),
    "gemini": (_gemini_key, _call_gemini),
}


def _available_providers():
    """List of (name, function) for providers with a configured key, in order."""
    available = []
    for name in PROVIDER_ORDER:
        key_fn, call_fn = _PROVIDER_CALLS[name]
        if key_fn():
            available.append((name, call_fn))
    return available


def has_provider():
    """True if at least one LLM provider is configured."""
    return bool(_available_providers())


def _extract_json_object(text):
    """Extract the first balanced JSON object {...} from a text, ignoring braces
    inside strings. Useful when the model wraps the JSON in prose."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _parse_result(raw_text):
    """Convert the raw LLM text into the standardized dict, or None if invalid."""
    if not raw_text:
        return None

    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()

    result = None
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Models like gpt-oss sometimes wrap the JSON in text/reasoning;
        # try to extract the first balanced {...} object.
        block = _extract_json_object(text)
        if block:
            try:
                result = json.loads(block)
            except json.JSONDecodeError:
                result = None
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


def _complete_with_providers(prompt, parse_fn, task="analysis", max_cycles=None, retry_wait=None):
    """
    Walk the chain of LLM providers (OpenRouter primary, Gemini fallback) sending
    `prompt` and validating the response with `parse_fn`. Each cycle tries every
    available provider; quota/limit errors fall through to the next one. If all
    fail, wait `retry_wait`s and repeat the chain up to `max_cycles` times.

    `max_cycles`/`retry_wait` default to the env-configured values
    (LLM_MAX_PROVIDER_CYCLES / LLM_QUOTA_RETRY_WAIT) when not passed explicitly.

    Returns the result of parse_fn (first valid response) or None if no provider
    is configured or all failed after the cycles.
    """
    if max_cycles is None:
        max_cycles = max_provider_cycles()
    if retry_wait is None:
        retry_wait = quota_retry_wait()

    providers = _available_providers()
    if not providers:
        logger.error("No LLM provider configured (set OPENROUTER_API_KEY and/or GEMINI_API_KEY in .env).")
        return None

    primary = providers[0][0]

    for cycle in range(1, max_cycles + 1):
        for name, call in providers:
            try:
                raw = call(prompt)
            except Exception as exc:
                level = "quota/limit" if _is_retryable_quota_error(exc) else "non-recoverable failure"
                msg = str(exc).replace("\n", " ")
                if len(msg) > 200:
                    msg = msg[:200] + "…"
                logger.warning(f"[{name}] {level} on {task}: {msg}. Trying next provider...")
                continue

            result = parse_fn(raw)
            if result is not None:
                if name != primary or cycle > 1:
                    logger.info(f"{task} obtained via fallback [{name}] (cycle {cycle}).")
                return result

            logger.warning(f"[{name}] returned invalid JSON on {task}. Trying next provider...")

        if cycle < max_cycles:
            logger.warning(
                f"All providers failed on {task} (cycle {cycle}/{max_cycles}). "
                f"Waiting {retry_wait:.0f}s and repeating the chain..."
            )
            if retry_wait > 0:
                time.sleep(retry_wait)

    logger.error(f"All LLM providers failed on {task} after the retry cycles.")
    return None


def analyze_match(job_info, candidate_profile):
    """
    Compare a job against the candidate profile using the LLM provider chain.
    Returns the dict {match_score, strengths, gaps, verdict} or None (so that main
    uses the simulated fallback analysis).
    """
    prompt = _build_prompt(job_info, candidate_profile)
    return _complete_with_providers(prompt, _parse_result, "Match analysis")


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
# Must stay above REPORT_MIN_CLT_SCORE (0.6) or default-CLT jobs would be hidden
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


def classify_contract(description, title=None, company=None, regex_signals=None):
    """
    Classify the employment type LOCALLY (no LLM) and return the standardized dict
    the scraper/reporter/history expect.

    Default-CLT: discard ONLY on explicit non-CLT evidence — strong contractor/PJ
    cues (USD-hourly pay, "Contract:" titles, 1099/C2C, PJ/CNPJ markers, a known
    contractor-marketplace company) or an internship marker. Anything else is
    accepted as assumed CLT.

    `regex_signals` is accepted for backward compatibility and ignored.
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


class ContractClassifier:
    """Adapter with the .classify(description, ...) interface the scraper uses,
    delegating to the local (regex-based) classify_contract."""

    def classify(self, description_text, title=None, company=None):
        return classify_contract(description_text, title=title, company=company)

import os
import json
import re
import time
import logging

import requests
from google import genai
from google.genai import types

from src.contract_classifier import explicit_negative_evidence

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"

# Primary provider: OpenRouter (free models). Gemini is the fallback.
# The order can be tuned here; both are tried on each cycle.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# List of free OpenRouter models, tried in order. The most popular ones
# (Llama 70B, Qwen) live in 429 ("rate-limited upstream") because the free
# pool is congested, so we prioritize strong but less contested models. On a
# 429 for one model, we move on to the next.
# Can be overridden with a single choice via the OPENROUTER_MODEL env var.
DEFAULT_OPENROUTER_MODELS = (
    "openai/gpt-oss-120b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
)
PROVIDER_ORDER = ("openrouter", "gemini")

# How many times to walk the full provider chain before giving up and falling
# back to main's simulated analysis. Raise to a high value for an almost
# "infinite until processed" behavior. Between cycles, wait QUOTA_RETRY_WAIT
# seconds.
MAX_PROVIDER_CYCLES = 3
QUOTA_RETRY_WAIT = 60.0

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


def _openrouter_models():
    # If OPENROUTER_MODEL is set in .env, use only it; otherwise the default list.
    override = os.getenv("OPENROUTER_MODEL")
    if override:
        return [override]
    return list(DEFAULT_OPENROUTER_MODELS)


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
3. List up to 4 candidate strengths that directly match the job requirements.
4. List the "gaps", i.e. the job's required or desired requirements that the candidate lacks or did not mention in their profile.
5. Write a friendly, honest and direct verdict (at most 3 sentences) advising what to focus on or whether it is worth applying.
Write the strengths, gaps and verdict in Portuguese (the candidate's language).

You MUST respond strictly in the JSON format below, with no explanatory blocks or markdown outside the JSON.
Desired response structure:
{{
  "match_score": <integer from 0 to 100>,
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

    for model in _openrouter_models():
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    # Generous output budget: reasoning models (e.g. gpt-oss)
                    # spend tokens "thinking" and, without this, the JSON comes
                    # out truncated.
                    "max_tokens": 2000,
                    # Minimize reasoning to leave budget for the final JSON.
                    "reasoning": {"effort": "low"},
                },
                timeout=60,
            )
            if resp.status_code != 200:
                last_error = RuntimeError(f"OpenRouter HTTP {resp.status_code} ({model}): {resp.text[:200]}")
                logger.warning(f"[openrouter:{model}] HTTP {resp.status_code}, trying next model...")
                continue
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if model != _openrouter_models()[0]:
                logger.info(f"[openrouter] using alternative model: {model}")
            return content
        except (KeyError, IndexError, TypeError) as exc:
            last_error = RuntimeError(f"Unexpected OpenRouter response ({model}): {exc}")
            continue
        except requests.RequestException as exc:
            last_error = RuntimeError(f"OpenRouter network error ({model}): {exc}")
            continue

    raise last_error


def _call_gemini(prompt):
    """Call Gemini. Returns the raw response text or raises an exception."""
    client = _get_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
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
        "strengths": [str(x) for x in strengths] if isinstance(strengths, list) else [],
        "gaps": [str(x) for x in gaps] if isinstance(gaps, list) else [],
        "verdict": str(verdict),
    }


def _complete_with_providers(prompt, parse_fn, task="analysis", max_cycles=MAX_PROVIDER_CYCLES, retry_wait=QUOTA_RETRY_WAIT):
    """
    Walk the chain of LLM providers (OpenRouter primary, Gemini fallback) sending
    `prompt` and validating the response with `parse_fn`. Each cycle tries every
    available provider; quota/limit errors fall through to the next one. If all
    fail, wait `retry_wait`s and repeat the chain up to `max_cycles` times.

    Returns the result of parse_fn (first valid response) or None if no provider
    is configured or all failed after the cycles.
    """
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
                logger.warning(f"[{name}] {level} on {task}: {exc}. Trying next provider...")
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
# Employment-type classification (CLT vs PJ/freelancer/internship) via LLM.
#
# Replaces the embedding similarity over the whole description, which diluted the
# contract signal (1-2 sentences among dozens about stack/requirements) and left
# ~90% of jobs "ambiguous". Here the LLM reads the description with the key rule
# of the Brazilian market: CLT is the implicit DEFAULT; we only discard when there
# is EXPLICIT evidence of a non-CLT type.
# ---------------------------------------------------------------------------

# Regimes considered non-CLT; a job is only discarded if one of them comes with
# confidence >= MIN_DISCARD_CONFIDENCE.
_NON_CLT_REGIMES = {"PJ", "FREELANCER", "ESTAGIO", "TEMPORARIO"}
_VALID_REGIMES = _NON_CLT_REGIMES | {"CLT", "INDEFINIDO"}
MIN_DISCARD_CONFIDENCE = 0.6


def _build_contract_prompt(title, company, description, regex_signals):
    hint = ""
    if regex_signals:
        hint = (
            "\nTextual signals detected by keyword search (use as support, but "
            "trust your own reading): " + ", ".join(regex_signals) + "\n"
        )
    return f"""
You are a specialist in Brazilian labor law and recruitment.
Your task is to classify the EMPLOYMENT TYPE of a job from its description.
The description may be written in Portuguese; analyze it as-is.

Job:
- Title: {title or 'N/A'}
- Company: {company or 'N/A'}
- Description:
{description or 'No description.'}
{hint}
Classification rules:
- "CLT": employment relationship / signed work card (carteira assinada) / permanent / employee / typical benefits (VR, VA, health plan, vacation, 13th salary).
- "PJ": legal entity (pessoa jurídica), requires CNPJ, invoice issuance, service provider, contractor.
- "FREELANCER": freelancer, self-employed, project/on-demand work with no employment bond.
- "ESTAGIO": internship position.
- "TEMPORARIO": temporary / fixed-term contract.
- "INDEFINIDO": the description gives NO hint about the employment type.

CRITICAL RULE: in Brazil, CLT is the default. Only classify as PJ, FREELANCER,
ESTAGIO or TEMPORARIO if there is EXPLICIT evidence in the text. Most CLT jobs
never write "CLT" — do not conclude "indefinido" just because of that; if there
are signs of an employment/permanent bond, classify as CLT. When in doubt
between CLT and INDEFINIDO, prefer CLT.

Respond STRICTLY in this JSON, with no markdown or text outside it:
{{
  "regime": "CLT|PJ|FREELANCER|ESTAGIO|TEMPORARIO|INDEFINIDO",
  "confidence": <number from 0.0 to 1.0>,
  "evidence": "<short excerpt from the description or a 1-sentence justification>"
}}
"""


def _parse_contract(raw_text):
    """Convert the raw LLM text into the dict {regime, confidence, evidence} or None."""
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
        block = _extract_json_object(text)
        if block:
            try:
                result = json.loads(block)
            except json.JSONDecodeError:
                result = None
    if not isinstance(result, dict):
        return None

    regime = str(result.get("regime", "")).strip().upper()
    if regime not in _VALID_REGIMES:
        return None

    try:
        confidence = float(result.get("confidence", 0.0))
    except (ValueError, TypeError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "regime": regime,
        "confidence": confidence,
        "evidence": str(result.get("evidence", "")).strip(),
    }


def classify_contract(description, title=None, company=None, regex_signals=None):
    """
    Classify the employment type via LLM and return the standardized dict that the
    scraper/reporter/history expect (same keys as the old classifier).

    Default-CLT logic: the job is ACCEPTED (accepted=True) for CLT and INDEFINIDO;
    it is only discarded when the regime is non-CLT with enough confidence. If no
    LLM provider responds, the job is KEPT by default (we never discard due to an
    infrastructure failure).
    """
    prompt = _build_contract_prompt(title, company, description, regex_signals or [])
    # A single pass through the chain, without long waits: classification is a
    # collection pre-filter; if the LLM fails, we keep the job (default CLT).
    result = _complete_with_providers(
        prompt, _parse_contract, "Contract classification",
        max_cycles=1, retry_wait=0,
    )

    if result is None:
        return {
            "inferred_contract_type": "INDEFINIDO",
            "accepted": True,
            "score_clt": "N/A",
            "score_non_clt": "N/A",
            "contract_margin": "N/A",
            "contract_evidence": "LLM unavailable; job kept by default (CLT assumed).",
        }

    regime = result["regime"]
    confidence = result["confidence"]
    discard = regime in _NON_CLT_REGIMES and confidence >= MIN_DISCARD_CONFIDENCE
    accepted = not discard

    evidence = (
        f"[LLM] regime={regime} confidence={confidence:.2f}. {result['evidence']}"
    ).strip()
    if regime in _NON_CLT_REGIMES and not discard:
        evidence += (
            f" (confidence below {MIN_DISCARD_CONFIDENCE:.2f}; "
            "kept conservatively as assumed CLT)."
        )

    return {
        "inferred_contract_type": "CLT" if accepted else regime,
        "accepted": accepted,
        # Reuse the report's score columns to expose the LLM confidence.
        "score_clt": round(confidence, 2) if regime in ("CLT", "INDEFINIDO") else "N/A",
        "score_non_clt": round(confidence, 2) if regime in _NON_CLT_REGIMES else "N/A",
        "contract_margin": "N/A",
        "contract_evidence": evidence,
    }


class LLMContractClassifier:
    """
    Adapter with the same .classify(description, ...) interface used by the
    scraper, delegating employment-type classification to the LLM chain
    (classify_contract). Kept as a class to mirror the embeddings-based
    ContractClassifier and allow a transparent swap in main.py.
    """

    def classify(self, description_text, title=None, company=None):
        signals = explicit_negative_evidence(description_text)
        return classify_contract(
            description_text, title=title, company=company, regex_signals=signals,
        )

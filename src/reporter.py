import logging

from src.settings import env_float

logger = logging.getLogger(__name__)

# Final-report relevance thresholds (defaults; overridable via the environment).
# A job only reaches the Markdown if it is both confidently CLT and aligned with
# the candidate profile:
# - CLT confidence (score_clt, 0.0-1.0) must be numeric and >= the CLT threshold.
#   "N/A" (non-CLT regime kept conservatively, or LLM unavailable) is excluded.
# - profile match (match_score, 0-100) must be >= the match threshold.
# Env overrides: REPORT_MIN_CLT_SCORE, REPORT_MIN_MATCH_SCORE.
# User rule: "abaixo de 0,7 no CLT não me interessa" and "score < 0,7 não me
# interessa". Both bars at 0.70. Override per deployment via the env vars below.
DEFAULT_MIN_CLT_SCORE = 0.7
DEFAULT_MIN_MATCH_SCORE = 70


def min_clt_score():
    return env_float("REPORT_MIN_CLT_SCORE", DEFAULT_MIN_CLT_SCORE)


def min_match_score():
    return env_float("REPORT_MIN_MATCH_SCORE", DEFAULT_MIN_MATCH_SCORE)


def _as_float(value):
    """Return value as float, or None if it is not a number (e.g. 'N/A')."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def passes_relevance_filter(job):
    """True if the job is confidently CLT and aligned with the profile."""
    clt_score = _as_float(job.get("score_clt"))
    if clt_score is None or clt_score < min_clt_score():
        return False
    match_score = _as_float(job.get("match_score")) or 0.0
    return match_score >= min_match_score()

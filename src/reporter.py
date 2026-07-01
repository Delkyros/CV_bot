import logging
from datetime import datetime

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


def format_list(items):
    if not items:
        return "- N/A"
    return "\n".join(f"- {item}" for item in items)


def table_text(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def generate_report(analyzed_jobs, output_path="vagas_filtradas.md"):
    """
    Generate a consolidated Markdown report, sorted by best match.
    """
    logger.info(f"Generating Markdown report for {len(analyzed_jobs)} analyzed jobs...")

    if not analyzed_jobs:
        logger.warning("No successfully analyzed job to generate the report.")
        return False

    clt_threshold = min_clt_score()
    match_threshold = min_match_score()

    relevant_jobs = [job for job in analyzed_jobs if passes_relevance_filter(job)]
    dropped = len(analyzed_jobs) - len(relevant_jobs)
    if dropped:
        logger.info(
            f"Relevance filter: {dropped} of {len(analyzed_jobs)} job(s) hidden "
            f"from the report (CLT score < {clt_threshold} or non-numeric, "
            f"or match score < {match_threshold})."
        )

    if not relevant_jobs:
        logger.warning(
            "No job passed the relevance filter "
            f"(CLT score >= {clt_threshold} and match score >= {match_threshold})."
        )

    sorted_jobs = sorted(
        relevant_jobs,
        key=lambda job: int(job.get("match_score", 0) or 0),
        reverse=True
    )

    lines = [
        "# Filtered jobs",
        "",
        f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Jobs shown: {len(sorted_jobs)} of {len(analyzed_jobs)} analyzed "
        f"(filter: CLT score >= {clt_threshold}, match score >= {match_threshold})",
        "",
        "| Score | Score CLT | Score Non-CLT | Margin | Job | Company | Contract | Model | Location |",
        "| ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |",
    ]

    for job in sorted_jobs:
        score = job.get("match_score", 0)
        title = job.get("job_title", "N/A")
        company = job.get("company", "N/A")
        link = job.get("job_link", "")
        title_link = f"[{table_text(title)}]({link})" if link else table_text(title)
        lines.append(
            "| {score} | {score_clt} | {score_non_clt} | {margin} | {title} | {company} | {contract} | {model} | {location} |".format(
                score=score,
                score_clt=table_text(job.get("score_clt", "N/A")),
                score_non_clt=table_text(job.get("score_non_clt", "N/A")),
                margin=table_text(job.get("contract_margin", "N/A")),
                title=title_link,
                company=table_text(company),
                contract=table_text(job.get("inferred_contract_type", job.get("contract_type", "N/A"))),
                model=table_text(job.get("workplace_type", "N/A")),
                location=table_text(job.get("location", "N/A")),
            )
        )

    lines.append("")
    lines.append("---")
    lines.append("")

    for idx, job in enumerate(sorted_jobs, start=1):
        title = job.get("job_title", "N/A")
        company = job.get("company", "N/A")
        link = job.get("job_link", "")
        lines.extend([
            f"## {idx}. {title} - {company}",
            "",
            f"- Match score: {job.get('match_score', 0)}/100",
            f"- Link: {link or 'N/A'}",
            f"- Inferred contract type: {job.get('inferred_contract_type', job.get('contract_type', 'N/A'))}",
            f"- Score CLT: {job.get('score_clt', 'N/A')}",
            f"- Score Non-CLT: {job.get('score_non_clt', 'N/A')}",
            f"- Contract margin: {job.get('contract_margin', 'N/A')}",
            f"- Contract evidence: {job.get('contract_evidence', job.get('contract_inference', 'N/A'))}",
            f"- Workplace type: {job.get('workplace_type', 'N/A')}",
            f"- Location: {job.get('location', 'N/A')}",
            "",
            "### Strengths",
            format_list(job.get("strengths", [])),
            "",
            "### Gaps",
            format_list(job.get("gaps", [])),
            "",
            "### Verdict",
            job.get("verdict", "N/A"),
            "",
            "---",
            "",
        ])

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).strip() + "\n")
        logger.info(f"Success! Report saved to '{output_path}'.")
        return True
    except Exception:
        logger.exception("Failed to generate the Markdown report")
        return False

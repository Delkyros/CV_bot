import logging
from datetime import datetime

logger = logging.getLogger(__name__)


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

    sorted_jobs = sorted(
        analyzed_jobs,
        key=lambda job: int(job.get("match_score", 0) or 0),
        reverse=True
    )

    lines = [
        "# Filtered jobs",
        "",
        f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Total new jobs analyzed: {len(sorted_jobs)}",
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

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def format_list(items):
    if not items:
        return "- N/A"
    return "\n".join(f"- {item}" for item in items)


def table_text(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def generate_report(vagas_analisadas, output_path="vagas_filtradas.md"):
    """
    Gera um relatorio consolidado em Markdown, ordenado por melhor match.
    """
    logger.info(f"Gerando relatorio Markdown para {len(vagas_analisadas)} vagas analisadas...")

    if not vagas_analisadas:
        logger.warning("Nenhuma vaga analisada com sucesso para gerar o relatorio.")
        return False

    vagas_ordenadas = sorted(
        vagas_analisadas,
        key=lambda vaga: int(vaga.get("nota_match", 0) or 0),
        reverse=True
    )

    linhas = [
        "# Vagas filtradas",
        "",
        f"Gerado em: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Total de vagas novas analisadas: {len(vagas_ordenadas)}",
        "",
        "| Nota | Score CLT | Score Nao-CLT | Margem | Vaga | Empresa | Contratacao | Modelo | Localizacao |",
        "| ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |",
    ]

    for vaga in vagas_ordenadas:
        nota = vaga.get("nota_match", 0)
        titulo = vaga.get("titulo_vaga", "N/A")
        empresa = vaga.get("empresa", "N/A")
        link = vaga.get("link_vaga", "")
        titulo_link = f"[{table_text(titulo)}]({link})" if link else table_text(titulo)
        linhas.append(
            "| {nota} | {score_clt} | {score_nao_clt} | {margem} | {titulo} | {empresa} | {contrato} | {modelo} | {localizacao} |".format(
                nota=nota,
                score_clt=table_text(vaga.get("score_clt", "N/A")),
                score_nao_clt=table_text(vaga.get("score_nao_clt", "N/A")),
                margem=table_text(vaga.get("margem_contratacao", "N/A")),
                titulo=titulo_link,
                empresa=table_text(empresa),
                contrato=table_text(vaga.get("tipo_contratacao_inferido", vaga.get("tipo_contratacao", "N/A"))),
                modelo=table_text(vaga.get("modelo_trabalho", "N/A")),
                localizacao=table_text(vaga.get("localizacao", "N/A")),
            )
        )

    linhas.append("")
    linhas.append("---")
    linhas.append("")

    for idx, vaga in enumerate(vagas_ordenadas, start=1):
        titulo = vaga.get("titulo_vaga", "N/A")
        empresa = vaga.get("empresa", "N/A")
        link = vaga.get("link_vaga", "")
        linhas.extend([
            f"## {idx}. {titulo} - {empresa}",
            "",
            f"- Nota de match: {vaga.get('nota_match', 0)}/100",
            f"- Link: {link or 'N/A'}",
            f"- Tipo de contratacao inferido: {vaga.get('tipo_contratacao_inferido', vaga.get('tipo_contratacao', 'N/A'))}",
            f"- Score CLT: {vaga.get('score_clt', 'N/A')}",
            f"- Score Nao-CLT: {vaga.get('score_nao_clt', 'N/A')}",
            f"- Margem contratacao: {vaga.get('margem_contratacao', 'N/A')}",
            f"- Evidencias de contratacao: {vaga.get('evidencias_contratacao', vaga.get('inferencia_contratacao', 'N/A'))}",
            f"- Modelo de trabalho: {vaga.get('modelo_trabalho', 'N/A')}",
            f"- Localizacao: {vaga.get('localizacao', 'N/A')}",
            "",
            "### Pontos fortes",
            format_list(vaga.get("pontos_fortes", [])),
            "",
            "### Gaps",
            format_list(vaga.get("gaps", [])),
            "",
            "### Veredicto",
            vaga.get("veredicto", "N/A"),
            "",
            "---",
            "",
        ])

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(linhas).strip() + "\n")
        logger.info(f"Sucesso! Relatorio salvo em '{output_path}'.")
        return True
    except Exception:
        logger.exception("Falha ao gerar relatorio Markdown")
        return False

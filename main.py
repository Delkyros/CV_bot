import os
import sys
import json
import logging
from datetime import datetime
import yaml
from dotenv import load_dotenv

# Garante que os arquivos do pacote src/ possam ser importados corretamente
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from src.scraper import scrape_linkedin_jobs
from src.matcher import analyze_match
from src.reporter import generate_report
from src.contract_classifier import ContractClassifier
from src.logging_config import setup_logging

logger = logging.getLogger(__name__)


_LABEL_ACRONIMOS = {
    "ia": "IA", "llm": "LLM", "llms": "LLMs", "nlp": "NLP", "ml": "ML",
    "mlops": "MLOps", "api": "API", "apis": "APIs", "etl": "ETL", "elt": "ELT",
    "aws": "AWS", "gcp": "GCP", "ci": "CI", "cd": "CD",
}
_LABEL_CONECTORES = {"e", "de", "da", "do", "para", "com"}


def _prettify_label(key):
    """Transforma uma chave snake_case (ex.: ia_e_llms) num rotulo legivel
    (ex.: 'IA e LLMs'), preservando acronimos e conectores em minusculo."""
    palavras = []
    for w in str(key).replace("_", " ").split():
        baixo = w.lower()
        if baixo in _LABEL_ACRONIMOS:
            palavras.append(_LABEL_ACRONIMOS[baixo])
        elif baixo in _LABEL_CONECTORES:
            palavras.append(baixo)
        else:
            palavras.append(w.capitalize())
    return " ".join(palavras)


def format_candidate_profile(perfil):
    """
    Converte o perfil_candidato (dict estruturado vindo do YAML ou texto puro,
    para retrocompatibilidade) em um texto corrido e legivel para o prompt do
    matcher. O Gemini recebe texto formatado, nao o repr de um dicionario.
    """
    if perfil is None:
        return ""
    if isinstance(perfil, str):
        return perfil.strip()
    if not isinstance(perfil, dict):
        return str(perfil).strip()

    secoes = []

    resumo = perfil.get("resumo_profissional")
    if resumo:
        secoes.append("Resumo Profissional:\n" + str(resumo).strip())

    competencias = perfil.get("competencias_tecnicas")
    if isinstance(competencias, dict):
        linhas = ["Competências Técnicas (Hard Skills):"]
        for categoria, itens in competencias.items():
            titulo = _prettify_label(categoria)
            if isinstance(itens, (list, tuple)):
                itens_txt = ", ".join(str(i) for i in itens)
            else:
                itens_txt = str(itens)
            linhas.append(f"- {titulo}: {itens_txt}")
        secoes.append("\n".join(linhas))
    elif competencias:
        secoes.append("Competências Técnicas (Hard Skills):\n" + str(competencias).strip())

    soft_skills = perfil.get("soft_skills")
    if isinstance(soft_skills, (list, tuple)):
        linhas = ["Soft Skills:"] + [f"- {s}" for s in soft_skills]
        secoes.append("\n".join(linhas))
    elif soft_skills:
        secoes.append("Soft Skills:\n" + str(soft_skills).strip())

    nivel = perfil.get("nivel_experiencia")
    if nivel:
        secoes.append(f"Nível de Experiência: {nivel}")

    return "\n\n".join(secoes).strip()


def load_job_history(history_path):
    if not os.path.exists(history_path):
        return {}

    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning(f"Nao foi possivel carregar histórico '{history_path}': {e}")

    return {}


def save_job_history(history_path, history, vagas_analisadas):
    now = datetime.now().isoformat(timespec="seconds")

    for vaga in vagas_analisadas:
        link = vaga.get("link_vaga")
        if not link:
            continue

        history[link] = {
            "titulo_vaga": vaga.get("titulo_vaga", "N/A"),
            "empresa": vaga.get("empresa", "N/A"),
            "localizacao": vaga.get("localizacao", "N/A"),
            "tipo_contratacao": vaga.get("tipo_contratacao", "N/A"),
            "tipo_contratacao_inferido": vaga.get("tipo_contratacao_inferido", "N/A"),
            "score_clt": vaga.get("score_clt", "N/A"),
            "score_nao_clt": vaga.get("score_nao_clt", "N/A"),
            "margem_contratacao": vaga.get("margem_contratacao", "N/A"),
            "evidencias_contratacao": vaga.get("evidencias_contratacao", "N/A"),
            "modelo_trabalho": vaga.get("modelo_trabalho", "N/A"),
            "nota_match": vaga.get("nota_match", 0),
            "primeira_vez_vista_em": history.get(link, {}).get("primeira_vez_vista_em", now),
            "ultima_vez_processada_em": now,
        }

    try:
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        logger.info(f"Historico atualizado em '{history_path}' ({len(history)} vagas conhecidas).")
        return True
    except Exception as e:
        logger.error(f"Falha ao salvar histórico '{history_path}': {e}")
        return False


def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("PIPELINE DE BUSCA E MATCH DE VAGAS DO LINKEDIN")
    logger.info("=" * 60)

    # 1. Carrega variáveis de ambiente (.env)
    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "sua_chave_do_gemini_aqui":
        logger.warning("GEMINI_API_KEY não configurada ou com valor padrão no arquivo .env.")
        logger.warning("Configure sua chave de API do Gemini para rodar a etapa de match de inteligência artificial.")
        logger.warning("O script continuará com a busca de vagas, mas a etapa de match usará uma nota padrão (simulada).")
    
    # 2. Carrega configurações do arquivo YAML
    config_path = "config/keywords.yaml"
    if not os.path.exists(config_path):
        logger.error(f"Arquivo de configuração '{config_path}' não encontrado!")
        sys.exit(1)

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Falha ao carregar o arquivo YAML '{config_path}': {e}")
        sys.exit(1)
        
    termos_busca = config.get("termos_busca", [])
    localizacao = config.get("localizacao", "Brasil")
    tipo_contratacao = config.get("tipo_contratacao", "CLT")
    tempo_publicacao = config.get("tempo_publicacao")
    filtros_busca = config.get("filtros_busca") or [{"modelo_trabalho": None, "localizacao": localizacao}]
    perfil_candidato = format_candidate_profile(config.get("perfil_candidato"))
    
    if not termos_busca:
        logger.error("Lista 'termos_busca' vazia no arquivo de configuração.")
        sys.exit(1)

    if not perfil_candidato:
        logger.error("'perfil_candidato' não configurado ou vazio no arquivo de configuração.")
        sys.exit(1)

    logger.info(f"Termos de busca encontrados: {termos_busca}")
    logger.info(f"Tipo de contratação: {tipo_contratacao}")
    logger.info(f"Filtro de tempo de publicação: {tempo_publicacao or 'sem filtro'}")
    logger.info(f"Filtros de busca: {filtros_busca}")
    logger.info(f"Perfil do candidato carregado ({len(perfil_candidato)} caracteres).")

    contract_classifier = None
    if str(tipo_contratacao).strip().lower() == "clt":
        contract_config_path = "config/contract_examples.yaml"
        if not os.path.exists(contract_config_path):
            logger.error(f"Arquivo de protótipos '{contract_config_path}' não encontrado!")
            sys.exit(1)

        try:
            with open(contract_config_path, "r", encoding="utf-8") as f:
                contract_config = yaml.safe_load(f)
            contract_classifier = ContractClassifier(contract_config)
        except Exception as e:
            logger.error(f"Falha ao inicializar classificador local de contratação: {e}")
            logger.error("Instale as dependências e garanta que o modelo de embeddings esteja disponível localmente.")
            sys.exit(1)

    history_path = "vagas_historico.json"
    historico_vagas = load_job_history(history_path)
    links_historico = set(historico_vagas.keys())
    logger.info(f"Vagas conhecidas carregadas do histórico: {len(links_historico)}")
    
    # 3. Executa o Scraper para coletar vagas do LinkedIn
    # Limitamos a 3 vagas por termo de busca para ser ágil e evitar bloqueios, mas pode ser expandido
    vagas_coletadas = []
    links_coletados = set()
    max_vagas_por_termo = 3
    
    for termo in termos_busca:
        for filtro in filtros_busca:
            localizacao_filtro = filtro.get("localizacao", localizacao)
            modelo_trabalho = filtro.get("modelo_trabalho")
            geo_id_filtro = filtro.get("geo_id")
            try:
                vagas = scrape_linkedin_jobs(
                    termo,
                    location=localizacao_filtro,
                    max_jobs=max_vagas_por_termo,
                    contract_type=tipo_contratacao,
                    workplace_type=modelo_trabalho,
                    excluded_links=links_historico | links_coletados,
                    contract_classifier=contract_classifier,
                    geo_id=geo_id_filtro,
                    time_filter=tempo_publicacao,
                )
                for vaga in vagas:
                    link_vaga = vaga.get("link_vaga")
                    if link_vaga in links_coletados:
                        logger.info(f"Vaga duplicada ignorada: {vaga.get('titulo_vaga')} | {vaga.get('empresa')}")
                        continue
                    links_coletados.add(link_vaga)
                    vagas_coletadas.append(vaga)
            except Exception:
                logger.exception(f"Erro ao buscar vagas para o termo '{termo}' com filtro {filtro}")
                continue

    total_vagas = len(vagas_coletadas)
    logger.info(f"Busca concluída! Total de vagas coletadas de todas as buscas: {total_vagas}")

    if total_vagas == 0:
        logger.info("Nenhuma vaga nova pôde ser coletada. Encerrando pipeline.")
        sys.exit(0)
        
    # 4. Executa o Matcher para analisar cada vaga coletada
    vagas_analisadas = []
    
    logger.info("=" * 40)
    logger.info("INICIANDO ANÁLISE DE MATCH")
    logger.info("=" * 40)

    for idx, vaga in enumerate(vagas_coletadas, start=1):
        logger.info(f"Analisando vaga {idx} de {total_vagas}: {vaga['titulo_vaga']} | Empresa: {vaga['empresa']}")

        resultado_analysis = None

        # Só executa a análise se a chave de API do Gemini for real/configurada
        if api_key and api_key != "sua_chave_do_gemini_aqui":
            try:
                resultado_analysis = analyze_match(vaga, perfil_candidato)
            except Exception:
                logger.exception("Falha na chamada da API para esta vaga")

        # Fallback inteligente (Resiliência) caso a API falhe ou a chave não esteja disponível
        if not resultado_analysis:
            if not api_key or api_key == "sua_chave_do_gemini_aqui":
                logger.warning("Usando análise simulada de fallback devido à falta da GEMINI_API_KEY.")
            else:
                logger.warning("Usando análise simulada de fallback devido a uma falha na chamada do Gemini.")
                
            # Cria um resultado amigável de simulação/fallback para que o pipeline não quebre
            # E para que o usuário possa testar o pipeline completo sem a chave se quiser
            resultado_analysis = {
                "nota_match": 50,  # Nota neutra
                "pontos_fortes": ["Vaga coletada com sucesso", "Disponível para avaliação"],
                "gaps": ["Análise do Gemini indisponível (Verifique sua GEMINI_API_KEY no .env)"],
                "veredicto": "Insira uma chave válida em GEMINI_API_KEY no arquivo .env para obter uma análise de inteligência artificial real desta vaga."
            }
            
        # Mescla os dados da vaga com a análise
        vaga_analisada = {**vaga, **resultado_analysis}
        vagas_analisadas.append(vaga_analisada)

        logger.info(f"Resultado: Nota {vaga_analisada['nota_match']}/100")
        
    # 5. Executa o Reporter para gerar o arquivo Markdown de saída
    vagas_salvas = generate_report(vagas_analisadas, output_path="vagas_filtradas.md")
    historico_salvo = save_job_history(history_path, historico_vagas, vagas_analisadas)
    
    if vagas_salvas and historico_salvo:
        logger.info("=" * 60)
        logger.info("PIPELINE CONCLUÍDO COM SUCESSO!")
        logger.info("O relatório 'vagas_filtradas.md' está disponível na raiz do projeto.")
        logger.info("O histórico 'vagas_historico.json' foi atualizado.")
        logger.info("=" * 60)
    else:
        logger.error("Falha ao gerar o relatório final ou atualizar o histórico.")
        sys.exit(1)

if __name__ == "__main__":
    main()

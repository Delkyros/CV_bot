import time
import random
import re
import logging
import urllib.parse
import unicodedata
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Lista de User-Agents realistas para alternar nas requisições
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0"
]

def get_headers():
    """Gera cabeçalhos HTTP aleatórios para simular um navegador real."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }


# Parametros de retentativa para contornar bloqueios temporarios (429) sem
# recorrer a paralelismo. Retentativa serial, aguardando RETRY_WAIT segundos.
MAX_RETRIES = 5
RETRY_WAIT = 5.0


def request_with_retry(url, headers=None, timeout=15, max_retries=MAX_RETRIES, retry_wait=RETRY_WAIT):
    """
    Faz GET com retentativa em caso de 429 (Too Many Requests) ou erro de rede,
    aguardando retry_wait segundos entre cada tentativa (sem paralelismo).

    Retorna o objeto Response (mesmo com status != 200, p. ex. ainda 429 apos
    esgotar as tentativas) ou None se todas as tentativas falharem por rede.
    """
    headers = headers or get_headers()
    last_response = None

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            last_response = response

            if response.status_code == 429:
                if attempt < max_retries:
                    logger.warning(f"429 (Too Many Requests). Tentativa {attempt}/{max_retries}. Aguardando {retry_wait:.0f}s e retentando...")
                    time.sleep(retry_wait)
                    continue
                logger.warning(f"429 persistente apos {max_retries} tentativas. Desistindo desta requisicao.")
                return response

            return response

        except requests.RequestException as exc:
            if attempt < max_retries:
                logger.warning(f"Falha de rede (tentativa {attempt}/{max_retries}): {exc}. Aguardando {retry_wait:.0f}s e retentando...")
                time.sleep(retry_wait)
                continue
            logger.error(f"Falha de rede apos {max_retries} tentativas: {exc}")

    return last_response


def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def workplace_matches(location_text, description_text, workplace_type, search_location):
    """
    Confirma os modelos aceitos: remoto no Brasil ou hibrido em Sao Jose-SC
    ou Florianopolis-SC.

    O modelo de trabalho (remoto/hibrido) ja e filtrado pelo parametro f_WT na
    URL de busca do LinkedIn, entao aqui validamos apenas a LOCALIZACAO real da
    vaga (que o LinkedIn nao restringe de forma confiavel pelo parametro
    location). Exigir a palavra literal "hibrido"/"remoto" no texto descartava
    vagas validas, e aceitar "brasil" vindo da busca deixava passar vagas dos EUA.
    """
    if not workplace_type:
        return True

    normalized_workplace = normalize_text(workplace_type)
    location_norm = normalize_text(location_text)

    if normalized_workplace == "remoto":
        # O pais ja e garantido pelo geoId na URL de busca (Brasil). Aqui apenas
        # rejeitamos vagas cuja localizacao mencione explicitamente outro pais,
        # sem exigir a palavra "brasil" (cidades como "Campinas, SP" nao a contem).
        foreign = [
            "estados unidos", "united states", "canada", "espanha", "spain",
            "portugal", "india", "mexico", "argentina", "reino unido",
            "republica dominicana", "alemanha", "franca",
        ]
        return not any(token in location_norm for token in foreign)

    if normalized_workplace in ("hibrido", "hybrid"):
        # Mantem apenas vagas hibridas em Sao Jose-SC / Florianopolis-SC.
        return any(
            token in location_norm
            for token in ["sao jose", "florianopolis", "floripa", "santa catarina"]
        )

    return True

def job_matches_filters(job_info, contract_type, workplace_type, search_location):
    return workplace_matches(
        job_info.get("localizacao", ""),
        job_info.get("descricao_completa", ""),
        workplace_type,
        search_location,
    )

def linkedin_time_filter(period):
    """
    Mapeia um periodo amigavel para o parametro f_TPR (time posted range) do
    LinkedIn, em segundos. Retorna None quando nao ha filtro de tempo.
    """
    normalized = normalize_text(period)
    mapping = {
        "24h": "r86400",
        "24 horas": "r86400",
        "dia": "r86400",
        "diario": "r86400",
        "semana": "r604800",
        "7 dias": "r604800",
        "semanal": "r604800",
        "mes": "r2592000",
        "30 dias": "r2592000",
        "mensal": "r2592000",
    }
    return mapping.get(normalized)


def linkedin_workplace_filter(workplace_type):
    normalized = normalize_text(workplace_type)
    if normalized == "remoto":
        return "2"
    if normalized in ("hibrido", "hybrid"):
        return "3"
    if normalized in ("presencial", "onsite", "on-site"):
        return "1"
    return None

def extract_job_id(url, card_element=None):
    """
    Tenta extrair o ID da vaga do link ou do próprio elemento do card HTML.
    """
    # Método 1: Atributo data-entity-urn do card
    if card_element:
        urn = card_element.get("data-entity-urn")
        if urn and "jobPosting:" in urn:
            match = re.search(r"jobPosting:(\d+)", urn)
            if match:
                return match.group(1)
                
    # Método 2: URL de visualização de vaga (/view/ID ou /jobs/view/ID)
    match = re.search(r"/view/(\d+)", url)
    if match:
        return match.group(1)
        
    match = re.search(r"currentJobId=(\d+)", url)
    if match:
        return match.group(1)
        
    # Método 3: Tenta pegar o último grupo de números após o último hífen ou barra antes dos parâmetros
    clean_url = url.split("?")[0]
    match = re.search(r"-(\d+)(?:/|$)", clean_url)
    if match:
        return match.group(1)
        
    return None

def fetch_job_description(job_id):
    """
    Busca a descrição detalhada da vaga no endpoint público do LinkedIn Guest API.
    """
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    headers = get_headers()

    try:
        # Pausa amigável antes de buscar a descrição para evitar 429
        time.sleep(random.uniform(1.0, 2.5))

        response = request_with_retry(url, headers=headers, timeout=15)
        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "sem resposta"
            logger.warning(f"Falha ao obter detalhes da vaga ID {job_id}. Status: {status}")
            return "Descrição não disponível devido a erro de conexão pública."
            
        soup = BeautifulSoup(response.content, "html.parser")
        
        # Encontra o container com a descrição da vaga
        # O endpoint /jobs-guest/jobs/api/jobPosting/ retorna um HTML compacto com a descrição
        desc_element = soup.find(class_="show-more-less-html__markup")
        if not desc_element:
            desc_element = soup.find(class_="description__text")
            
        if desc_element:
            # Mantém alguma quebra de linha de parágrafo e remove tags HTML desnecessárias
            for br in desc_element.find_all("br"):
                br.replace_with("\n")
            for p in desc_element.find_all("p"):
                p.append("\n")
            for li in desc_element.find_all("li"):
                li.insert(0, "- ")
                li.append("\n")
                
            text = desc_element.get_text()
            # Remove espaçamentos extras mantendo quebras de linha limpas
            text = re.sub(r'\n\s*\n', '\n\n', text).strip()
            return text
            
        # Fallback: Se não achar as classes conhecidas, pega o texto do body todo
        body_text = soup.get_text().strip()
        if len(body_text) > 100:
            return body_text
            
        return "Descrição indisponível no HTML retornado."
        
    except Exception:
        logger.exception(f"Falha ao buscar descrição da vaga ID {job_id}")
        return "Erro ao extrair descrição da vaga."

def scrape_linkedin_jobs(
    keyword,
    location="Brasil",
    max_jobs=5,
    contract_type=None,
    workplace_type=None,
    excluded_links=None,
    contract_classifier=None,
    geo_id=None,
    time_filter=None,
    max_pages=10,
):
    """
    Busca vagas no LinkedIn utilizando o Guest API de busca pública.
    Retorna uma lista de dicionários com as informações coletadas.

    O parametro geo_id (geoId do LinkedIn) e o que realmente restringe o pais da
    busca; o parametro de texto `location` sozinho nao e respeitado de forma
    confiavel pela Guest API (retorna vagas dos EUA mesmo com location=Brasil).
    """
    search_keyword = keyword
    logger.info(f"Iniciando busca pública para: '{search_keyword}' em '{location}' (Modelo: {workplace_type or 'qualquer'} | Limite: {max_jobs} vagas)")

    jobs = []
    start = 0
    pages = 0
    headers = get_headers()
    excluded_links = set(excluded_links or [])

    # URL de busca Guest do LinkedIn
    encoded_keyword = urllib.parse.quote(search_keyword)
    encoded_location = urllib.parse.quote(location)
    workplace_filter = linkedin_workplace_filter(workplace_type)
    tpr_filter = linkedin_time_filter(time_filter)

    while len(jobs) < max_jobs and pages < max_pages:
        pages += 1
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={encoded_keyword}&location={encoded_location}&start={start}"
        if geo_id:
            url += f"&geoId={geo_id}"
        if workplace_filter:
            url += f"&f_WT={workplace_filter}"
        if tpr_filter:
            url += f"&f_TPR={tpr_filter}"

        try:
            time.sleep(random.uniform(1.5, 3.0))
            # Retentativa serial em 429/erro de rede (sem paralelismo).
            response = request_with_retry(url, headers=headers, timeout=15)

            if response is None:
                logger.error("Falha na busca apos multiplas tentativas. Encerrando este termo.")
                break

            if response.status_code != 200:
                logger.error(f"Falha na busca. Status code: {response.status_code}")
                break

            soup = BeautifulSoup(response.content, "html.parser")
            # Seleciona apenas os contêineres de vaga de nível superior. A âncora ^...$
            # evita casar elementos-filhos como "base-card__full-link" ou
            # "base-search-card__title", que antes inflavam a lista com ~5 fantasmas
            # por vaga e geravam avisos de "ID não encontrado" em massa.
            cards = soup.find_all(class_=re.compile(r"^(base-card|base-search-card)$"))
            
            if not cards:
                logger.info("Nenhuma vaga encontrada nesta página de busca ou fim dos resultados.")
                break

            logger.info(f"Encontrados {len(cards)} cards na página atual. Processando...")
            
            for card in cards:
                if len(jobs) >= max_jobs:
                    break
                    
                try:
                    # Título da vaga
                    title_elem = card.find(class_=re.compile(r"base-search-card__title|job-search-card__title"))
                    title = title_elem.get_text().strip() if title_elem else "Título não identificado"
                    
                    # Empresa
                    company_elem = card.find(class_=re.compile(r"base-search-card__subtitle|job-search-card__subtitle"))
                    company = company_elem.get_text().strip() if company_elem else "Empresa não identificada"
                    
                    # Link da vaga
                    link_elem = card.find("a", class_=re.compile(r"base-card__full-link|base-search-card__full-link"))
                    link = link_elem["href"].split("?")[0] if link_elem and "href" in link_elem.attrs else ""
                    
                    # Localização
                    loc_elem = card.find(class_=re.compile(r"job-search-card__location"))
                    loc = loc_elem.get_text().strip() if loc_elem else location
                    
                    # ID da Vaga para buscar descrição completa
                    job_id = extract_job_id(link, card)
                    
                    if not job_id:
                        # Se não conseguir o ID da vaga, descarta ou tenta usar o link como fallback
                        logger.warning(f"Não foi possível extrair ID para a vaga: {title} - {company}. Pulando.")
                        continue

                    job_link = f"https://www.linkedin.com/jobs/view/{job_id}"
                    if job_link in excluded_links:
                        logger.info(f"Vaga ja conhecida (histórico), pulando: {title} | {company} ({job_id})")
                        continue

                    # Filtra modelo/localizacao ANTES de baixar a descricao: a
                    # localizacao ja vem no card, entao evitamos requests (e risco
                    # de 429) baixando descricoes de vagas que serao descartadas.
                    if not workplace_matches(loc, "", workplace_type, location):
                        logger.info(f"Vaga fora do modelo/localidade alvo, pulando: {title} | {company} | {loc}")
                        continue

                    logger.info(f"Coletando descrição da vaga: {title} | {company} (ID: {job_id})...")
                    descricao = fetch_job_description(job_id)
                    contract_inference = {
                        "tipo_contratacao_inferido": contract_type or "N/A",
                        "aceita": True,
                        "score_clt": "N/A",
                        "score_nao_clt": "N/A",
                        "margem_contratacao": "N/A",
                        "evidencias_contratacao": "Classificador de contratacao nao configurado.",
                    }
                    if normalize_text(contract_type) == "clt":
                        if not contract_classifier:
                            raise RuntimeError("Classificador local de contratacao CLT nao foi inicializado.")
                        contract_inference = contract_classifier.classify(descricao)
                    
                    job_info = {
                        "titulo_vaga": title,
                        "empresa": company,
                        "localizacao": loc,
                        "modelo_trabalho": workplace_type or "N/A",
                        "tipo_contratacao": contract_inference.get("tipo_contratacao_inferido", contract_type or "N/A"),
                        "tipo_contratacao_inferido": contract_inference.get("tipo_contratacao_inferido", "N/A"),
                        "score_clt": contract_inference.get("score_clt", "N/A"),
                        "score_nao_clt": contract_inference.get("score_nao_clt", "N/A"),
                        "margem_contratacao": contract_inference.get("margem_contratacao", "N/A"),
                        "inferencia_contratacao": contract_inference.get("evidencias_contratacao", "N/A"),
                        "evidencias_contratacao": contract_inference.get("evidencias_contratacao", "N/A"),
                        "link_vaga": job_link,
                        "descricao_completa": descricao
                    }

                    if not contract_inference.get("aceita", True):
                        logger.info(
                            "Vaga descartada por contratacao {tipo}: {titulo} | {empresa}. "
                            "Scores: CLT {clt} | Nao-CLT {nao_clt} | Margem {margem}. {evidencias}".format(
                                tipo=job_info["tipo_contratacao_inferido"],
                                titulo=title,
                                empresa=company,
                                clt=job_info["score_clt"],
                                nao_clt=job_info["score_nao_clt"],
                                margem=job_info["margem_contratacao"],
                                evidencias=job_info["evidencias_contratacao"],
                            )
                        )
                        continue

                    jobs.append(job_info)
                    excluded_links.add(job_link)

                except Exception:
                    logger.exception("Falha ao processar um card de vaga")
                    continue

            # Avanca exatamente pelo numero de cards vistos. Antes incrementava
            # +25 fixo enquanto a pagina retornava ~10 vagas, pulando resultados.
            start += len(cards)
            
        except Exception:
            logger.exception("Falha ao realizar requisição de busca")
            break

    logger.info(f"Concluído! Total de vagas coletadas para '{search_keyword}': {len(jobs)}")
    return jobs

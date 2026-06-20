import time
import random
import re
import urllib.parse
import unicodedata
import requests
from bs4 import BeautifulSoup

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
    """
    if not workplace_type:
        return True

    normalized_workplace = normalize_text(workplace_type)
    combined = normalize_text(f"{location_text} {description_text}")
    normalized_location = normalize_text(search_location)

    if normalized_workplace == "remoto":
        is_remote = any(token in combined for token in ["remoto", "remote", "home office"])
        is_brazil = any(token in combined for token in ["brasil", "brazil"]) or "brasil" in normalized_location
        return is_remote and is_brazil

    if normalized_workplace in ("hibrido", "hybrid"):
        is_hybrid = any(token in combined for token in ["hibrido", "hybrid"])
        allowed_city = any(
            token in combined or token in normalized_location
            for token in ["sao jose", "florianopolis", "floripa"]
        )
        allowed_state = any(token in combined or token in normalized_location for token in ["santa catarina", " sc", "-sc"])
        return is_hybrid and allowed_city and allowed_state

    return True

def job_matches_filters(job_info, contract_type, workplace_type, search_location):
    return workplace_matches(
        job_info.get("localizacao", ""),
        job_info.get("descricao_completa", ""),
        workplace_type,
        search_location,
    )

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
        
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"  [Erro] Falha ao obter detalhes da vaga ID {job_id}. Status: {response.status_code}")
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
        
    except Exception as e:
        print(f"  [Erro Exception] Falha ao buscar descrição da vaga ID {job_id}: {e}")
        return "Erro ao extrair descrição da vaga."

def scrape_linkedin_jobs(
    keyword,
    location="Brasil",
    max_jobs=5,
    contract_type=None,
    workplace_type=None,
    excluded_links=None,
    contract_classifier=None,
):
    """
    Busca vagas no LinkedIn utilizando o Guest API de busca pública.
    Retorna uma lista de dicionários com as informações coletadas.
    """
    search_keyword = keyword
    print(f"\n[Scraper] Iniciando busca pública para: '{search_keyword}' em '{location}' (Modelo: {workplace_type or 'qualquer'} | Limite: {max_jobs} vagas)")
    
    jobs = []
    start = 0
    headers = get_headers()
    excluded_links = set(excluded_links or [])
    
    # URL de busca Guest do LinkedIn
    encoded_keyword = urllib.parse.quote(search_keyword)
    encoded_location = urllib.parse.quote(location)
    workplace_filter = linkedin_workplace_filter(workplace_type)
    
    while len(jobs) < max_jobs:
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={encoded_keyword}&location={encoded_location}&start={start}"
        if workplace_filter:
            url += f"&f_WT={workplace_filter}"
        
        try:
            time.sleep(random.uniform(1.5, 3.0))
            response = requests.get(url, headers=headers, timeout=15)
            
            if response.status_code == 429:
                print("  [Alerta] LinkedIn retornou status 429 (Too Many Requests). Aguardando pausa maior...")
                time.sleep(10)
                continue
                
            if response.status_code != 200:
                print(f"  [Erro] Falha na busca. Status code: {response.status_code}")
                break
                
            soup = BeautifulSoup(response.content, "html.parser")
            cards = soup.find_all(class_=re.compile(r"base-card|base-search-card"))
            
            if not cards:
                print("  [Aviso] Nenhuma vaga encontrada nesta página de busca ou fim dos resultados.")
                break
                
            print(f"  [Scraper] Encontrados {len(cards)} cards na página atual. Processando...")
            
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
                        print(f"  [Aviso] Não foi possível extrair ID para a vaga: {title} - {company}. Pulando.")
                        continue

                    job_link = f"https://www.linkedin.com/jobs/view/{job_id}"
                    if job_link in excluded_links:
                        print(f"  [Historico] Vaga ja conhecida, pulando: {title} | {company} ({job_id})")
                        continue
                        
                    print(f"  -> Coletando descrição da vaga: {title} | {company} (ID: {job_id})...")
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
                        print(f"  [Filtro] Vaga descartada por contratacao {job_info['tipo_contratacao_inferido']}: {title} | {company}")
                        print(
                            "           Scores: CLT {clt} | Nao-CLT {nao_clt} | Margem {margem}. {evidencias}".format(
                                clt=job_info["score_clt"],
                                nao_clt=job_info["score_nao_clt"],
                                margem=job_info["margem_contratacao"],
                                evidencias=job_info["evidencias_contratacao"],
                            )
                        )
                        continue

                    if not job_matches_filters(job_info, contract_type, workplace_type, location):
                        print(f"  [Filtro] Vaga descartada por não atender CLT/modelo/localidade: {title} | {company} | {loc}")
                        continue

                    jobs.append(job_info)
                    excluded_links.add(job_link)
                    
                except Exception as card_err:
                    print(f"  [Erro] Falha ao processar um card de vaga: {card_err}")
                    continue
                    
            # Prepara para ir para a próxima página de resultados
            start += 25
            
        except Exception as e:
            print(f"  [Erro Exception] Falha ao realizar requisição de busca: {e}")
            break
            
    print(f"[Scraper] Concluído! Total de vagas coletadas para '{search_keyword}': {len(jobs)}")
    return jobs

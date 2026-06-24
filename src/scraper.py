import time
import random
import re
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup

from src.text_signals import normalize_text

logger = logging.getLogger(__name__)

# List of realistic User-Agents to rotate across requests
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0"
]

def get_headers():
    """Generate random HTTP headers to mimic a real browser."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }


# Retry parameters to work around temporary blocks (429) without resorting to
# parallelism. Serial retry, waiting RETRY_WAIT seconds.
MAX_RETRIES = 5
RETRY_WAIT = 5.0


def request_with_retry(url, headers=None, timeout=15, max_retries=MAX_RETRIES, retry_wait=RETRY_WAIT):
    """
    Perform a GET with retry on a 429 (Too Many Requests) or network error,
    waiting retry_wait seconds between each attempt (no parallelism).

    Returns the Response object (even with status != 200, e.g. still 429 after
    exhausting the attempts) or None if all attempts fail due to network.
    """
    headers = headers or get_headers()
    last_response = None

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            last_response = response

            if response.status_code == 429:
                if attempt < max_retries:
                    logger.warning(f"429 (Too Many Requests). Attempt {attempt}/{max_retries}. Waiting {retry_wait:.0f}s and retrying...")
                    time.sleep(retry_wait)
                    continue
                logger.warning(f"Persistent 429 after {max_retries} attempts. Giving up on this request.")
                return response

            return response

        except requests.RequestException as exc:
            if attempt < max_retries:
                logger.warning(f"Network failure (attempt {attempt}/{max_retries}): {exc}. Waiting {retry_wait:.0f}s and retrying...")
                time.sleep(retry_wait)
                continue
            logger.error(f"Network failure after {max_retries} attempts: {exc}")

    return last_response


def workplace_matches(location_text, description_text, workplace_type, search_location):
    """
    Confirm the accepted workplace models: remote in Brazil or hybrid in Sao
    Jose-SC or Florianopolis-SC.

    The workplace type (remote/hybrid) is already filtered by the f_WT parameter
    in the LinkedIn search URL, so here we only validate the job's actual
    LOCATION (which LinkedIn does not reliably restrict via the location
    parameter). Requiring the literal word "hibrido"/"remoto" in the text
    discarded valid jobs, and accepting "brasil" from the search let US jobs slip
    through.
    """
    if not workplace_type:
        return True

    normalized_workplace = normalize_text(workplace_type)
    location_norm = normalize_text(location_text)

    if normalized_workplace == "remoto":
        # The country is already guaranteed by the geoId in the search URL
        # (Brazil). Here we only reject jobs whose location explicitly mentions
        # another country, without requiring the word "brasil" (cities like
        # "Campinas, SP" do not contain it).
        foreign = [
            "estados unidos", "united states", "canada", "espanha", "spain",
            "portugal", "india", "mexico", "argentina", "reino unido",
            "republica dominicana", "alemanha", "franca",
        ]
        return not any(token in location_norm for token in foreign)

    if normalized_workplace in ("hibrido", "hybrid"):
        # Keep only hybrid jobs in Sao Jose-SC / Florianopolis-SC.
        return any(
            token in location_norm
            for token in ["sao jose", "florianopolis", "floripa", "santa catarina"]
        )

    return True

def linkedin_time_filter(period):
    """
    Map a friendly period to LinkedIn's f_TPR (time posted range) parameter, in
    seconds. Returns None when there is no time filter.
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
    Try to extract the job ID from the link or from the HTML card element itself.
    """
    # Method 1: data-entity-urn attribute of the card
    if card_element:
        urn = card_element.get("data-entity-urn")
        if urn and "jobPosting:" in urn:
            match = re.search(r"jobPosting:(\d+)", urn)
            if match:
                return match.group(1)

    # Method 2: job view URL (/view/ID or /jobs/view/ID)
    match = re.search(r"/view/(\d+)", url)
    if match:
        return match.group(1)

    match = re.search(r"currentJobId=(\d+)", url)
    if match:
        return match.group(1)

    # Method 3: try to grab the last group of digits after the last hyphen or
    # slash before the parameters
    clean_url = url.split("?")[0]
    match = re.search(r"-(\d+)(?:/|$)", clean_url)
    if match:
        return match.group(1)

    return None

def fetch_job_description(job_id):
    """
    Fetch the detailed job description from the public LinkedIn Guest API endpoint.
    """
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    headers = get_headers()

    try:
        # Friendly pause before fetching the description to avoid 429
        time.sleep(random.uniform(1.0, 2.5))

        response = request_with_retry(url, headers=headers, timeout=15)
        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "no response"
            logger.warning(f"Failed to get details for job ID {job_id}. Status: {status}")
            return "Description unavailable due to a public connection error."

        soup = BeautifulSoup(response.content, "html.parser")

        # Find the container with the job description
        # The /jobs-guest/jobs/api/jobPosting/ endpoint returns compact HTML with
        # the description
        desc_element = soup.find(class_="show-more-less-html__markup")
        if not desc_element:
            desc_element = soup.find(class_="description__text")

        if desc_element:
            # Keep some paragraph line breaks and remove unnecessary HTML tags
            for br in desc_element.find_all("br"):
                br.replace_with("\n")
            for p in desc_element.find_all("p"):
                p.append("\n")
            for li in desc_element.find_all("li"):
                li.insert(0, "- ")
                li.append("\n")

            text = desc_element.get_text()
            # Remove extra whitespace while keeping clean line breaks
            text = re.sub(r'\n\s*\n', '\n\n', text).strip()
            return text

        # Fallback: if the known classes are not found, take the whole body text
        body_text = soup.get_text().strip()
        if len(body_text) > 100:
            return body_text

        return "Description unavailable in the returned HTML."

    except Exception:
        logger.exception(f"Failed to fetch description for job ID {job_id}")
        return "Error extracting the job description."

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
    Search LinkedIn jobs using the public search Guest API.
    Returns a list of dictionaries with the collected information.

    The geo_id parameter (LinkedIn geoId) is what actually restricts the search
    country; the `location` text parameter alone is not reliably honored by the
    Guest API (it returns US jobs even with location=Brasil).
    """
    search_keyword = keyword
    logger.info(f"Starting public search for: '{search_keyword}' in '{location}' (Model: {workplace_type or 'any'} | Limit: {max_jobs} jobs)")

    jobs = []
    start = 0
    pages = 0
    headers = get_headers()
    excluded_links = set(excluded_links or [])

    # LinkedIn Guest search URL
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
            # Serial retry on 429/network error (no parallelism).
            response = request_with_retry(url, headers=headers, timeout=15)

            if response is None:
                logger.error("Search failed after multiple attempts. Ending this term.")
                break

            if response.status_code != 200:
                logger.error(f"Search failed. Status code: {response.status_code}")
                break

            soup = BeautifulSoup(response.content, "html.parser")
            # Select only the top-level job containers. The ^...$ anchor avoids
            # matching child elements like "base-card__full-link" or
            # "base-search-card__title", which used to inflate the list with ~5
            # ghosts per job and generated mass "ID not found" warnings.
            cards = soup.find_all(class_=re.compile(r"^(base-card|base-search-card)$"))

            if not cards:
                logger.info("No jobs found on this search page or end of results.")
                break

            logger.info(f"Found {len(cards)} cards on the current page. Processing...")

            for card in cards:
                if len(jobs) >= max_jobs:
                    break

                try:
                    # Job title
                    title_elem = card.find(class_=re.compile(r"base-search-card__title|job-search-card__title"))
                    title = title_elem.get_text().strip() if title_elem else "Unidentified title"

                    # Company
                    company_elem = card.find(class_=re.compile(r"base-search-card__subtitle|job-search-card__subtitle"))
                    company = company_elem.get_text().strip() if company_elem else "Unidentified company"

                    # Job link
                    link_elem = card.find("a", class_=re.compile(r"base-card__full-link|base-search-card__full-link"))
                    link = link_elem["href"].split("?")[0] if link_elem and "href" in link_elem.attrs else ""

                    # Location
                    loc_elem = card.find(class_=re.compile(r"job-search-card__location"))
                    loc = loc_elem.get_text().strip() if loc_elem else location

                    # Job ID used to fetch the full description
                    job_id = extract_job_id(link, card)

                    if not job_id:
                        # If we cannot get the job ID, discard it or try the link as fallback
                        logger.warning(f"Could not extract ID for the job: {title} - {company}. Skipping.")
                        continue

                    job_link = f"https://www.linkedin.com/jobs/view/{job_id}"
                    if job_link in excluded_links:
                        logger.info(f"Job already known (history), skipping: {title} | {company} ({job_id})")
                        continue

                    # Filter model/location BEFORE downloading the description: the
                    # location already comes in the card, so we avoid requests (and
                    # the risk of 429) downloading descriptions of jobs that will be
                    # discarded.
                    if not workplace_matches(loc, "", workplace_type, location):
                        logger.info(f"Job outside the target model/location, skipping: {title} | {company} | {loc}")
                        continue

                    logger.info(f"Collecting job description: {title} | {company} (ID: {job_id})...")
                    description = fetch_job_description(job_id)
                    contract_inference = {
                        "inferred_contract_type": contract_type or "N/A",
                        "accepted": True,
                        "score_clt": "N/A",
                        "score_non_clt": "N/A",
                        "contract_margin": "N/A",
                        "contract_evidence": "Contract classifier not configured.",
                    }
                    if normalize_text(contract_type) == "clt":
                        if not contract_classifier:
                            raise RuntimeError("CLT contract classifier was not initialized.")
                        contract_inference = contract_classifier.classify(description, title=title, company=company)

                    job_info = {
                        "job_title": title,
                        "company": company,
                        "location": loc,
                        "workplace_type": workplace_type or "N/A",
                        "contract_type": contract_inference.get("inferred_contract_type", contract_type or "N/A"),
                        "inferred_contract_type": contract_inference.get("inferred_contract_type", "N/A"),
                        "score_clt": contract_inference.get("score_clt", "N/A"),
                        "score_non_clt": contract_inference.get("score_non_clt", "N/A"),
                        "contract_margin": contract_inference.get("contract_margin", "N/A"),
                        "contract_inference": contract_inference.get("contract_evidence", "N/A"),
                        "contract_evidence": contract_inference.get("contract_evidence", "N/A"),
                        "job_link": job_link,
                        "full_description": description
                    }

                    if not contract_inference.get("accepted", True):
                        logger.info(
                            "Job discarded by contract type {type}: {title} | {company}. "
                            "Scores: CLT {clt} | Non-CLT {non_clt} | Margin {margin}. {evidence}".format(
                                type=job_info["inferred_contract_type"],
                                title=title,
                                company=company,
                                clt=job_info["score_clt"],
                                non_clt=job_info["score_non_clt"],
                                margin=job_info["contract_margin"],
                                evidence=job_info["contract_evidence"],
                            )
                        )
                        continue

                    jobs.append(job_info)
                    excluded_links.add(job_link)

                except Exception:
                    logger.exception("Failed to process a job card")
                    continue

            # Advance by exactly the number of cards seen. It used to increment a
            # fixed +25 while the page returned ~10 jobs, skipping results.
            start += len(cards)

        except Exception:
            logger.exception("Failed to perform the search request")
            break

    logger.info(f"Done! Total jobs collected for '{search_keyword}': {len(jobs)}")
    return jobs

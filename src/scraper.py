import time
import random
import re
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup

from src.text_signals import normalize_text
from src.settings import env_float, env_int

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
# parallelism. Serial retry, waiting the configured wait between attempts.
# Defaults; overridable via SCRAPER_MAX_RETRIES / SCRAPER_RETRY_WAIT /
# SCRAPER_REQUEST_TIMEOUT / SCRAPER_MAX_PAGES.
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_WAIT = 5.0
DEFAULT_REQUEST_TIMEOUT = 15
DEFAULT_MAX_PAGES = 10

# Friendly random pause (seconds) between requests to avoid 429. Range is
# overridable via SCRAPER_MIN_REQUEST_DELAY / SCRAPER_MAX_REQUEST_DELAY.
DEFAULT_MIN_REQUEST_DELAY = 1.0
DEFAULT_MAX_REQUEST_DELAY = 3.0


def scraper_max_retries():
    return env_int("SCRAPER_MAX_RETRIES", DEFAULT_MAX_RETRIES)


def scraper_retry_wait():
    return env_float("SCRAPER_RETRY_WAIT", DEFAULT_RETRY_WAIT)


def scraper_request_timeout():
    return env_int("SCRAPER_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)


def scraper_max_pages():
    return env_int("SCRAPER_MAX_PAGES", DEFAULT_MAX_PAGES)


def _sleep_between_requests():
    """Pause a random interval within the configured request-delay range."""
    low = env_float("SCRAPER_MIN_REQUEST_DELAY", DEFAULT_MIN_REQUEST_DELAY)
    high = env_float("SCRAPER_MAX_REQUEST_DELAY", DEFAULT_MAX_REQUEST_DELAY)
    if high < low:
        low, high = high, low
    time.sleep(random.uniform(low, high))


def request_with_retry(url, headers=None, timeout=None, max_retries=None, retry_wait=None):
    """
    Perform a GET with retry on a 429 (Too Many Requests) or network error,
    waiting retry_wait seconds between each attempt (no parallelism).

    timeout/max_retries/retry_wait default to the env-configured values when not
    passed explicitly.

    Returns the Response object (even with status != 200, e.g. still 429 after
    exhausting the attempts) or None if all attempts fail due to network.
    """
    headers = headers or get_headers()
    if timeout is None:
        timeout = scraper_request_timeout()
    if max_retries is None:
        max_retries = scraper_max_retries()
    if retry_wait is None:
        retry_wait = scraper_retry_wait()
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
        # Keep ONLY hybrid jobs in Sao Jose-SC or Florianopolis-SC -- NOT the
        # whole state of Santa Catarina. (Other SC cities like Criciuma,
        # Joinville and Mafra used to slip through because the check accepted any
        # "santa catarina"/"sc" location.)
        # Florianopolis/Floripa are unambiguously in Santa Catarina.
        if "florianopolis" in location_norm or "floripa" in location_norm:
            return True
        if "sao jose" in location_norm:
            # Reject the Sao Paulo homonyms ("Sao Jose dos Campos", "Sao Jose do
            # Rio Preto") that share the same prefix.
            if "dos campos" in location_norm or "do rio preto" in location_norm:
                return False
            # Require an explicit Santa Catarina marker so other "Sao Jose"
            # homonyms (in other states) don't pass.
            return "santa catarina" in location_norm or bool(re.search(r"\bsc\b", location_norm))
        # Any other location (including other SC cities, or a bare "Santa
        # Catarina" with no city) is not one of our two hybrid hubs -> reject.
        return False

    return True


# Strong, explicit signals that a posting is HYBRID or ON-SITE, matched against
# the accent-stripped, lowercased description.
_ONSITE_HYBRID_PATTERNS = (
    r"\bhibrid[oa]s?\b",
    r"\bhybrid\b",
    r"\bpresenci(?:al|ais)\b",
    r"\bon[\s-]?site\b",
    r"\bin[\s-]?office\b",
    r"work location of this role is hybrid",
    r"\bdias? no escritorio\b",
)
# Any indication the role CAN be done remotely. Its presence vetoes the guard
# below: a posting that mentions "hibrido" only in passing while also offering
# remote work (e.g. "remoto ou hibrido") must NOT be rejected.
_REMOTE_PATTERNS = (
    r"\bremot[oa]s?\b",
    r"\bremote\b",
    r"\bremotamente\b",
    r"home[\s-]?office",
    r"\bhomeoffice\b",
    r"\bteletrabalho\b",
    r"\banywhere\b",
    r"de qualquer lugar",
)


def description_conflicts_with_remote(description_text):
    """
    True when a job description EXPLICITLY declares a hybrid/on-site model and
    gives no sign of remote work.

    LinkedIn's f_WT=2 (remote) filter is leaky and occasionally returns
    hybrid/on-site jobs. A remote search trusts f_WT for the model and accepts
    any Brazilian location, so such a leak gets mislabeled "remoto" and slips
    through (see history: hybrid São Paulo jobs flagged "Localidade/Modelo
    incorreto"). This is the only model signal the Guest API exposes per job.

    Conservative by design: a posting that mentions any remote possibility, or
    that says nothing about the work model, is left for f_WT to decide and
    returns False.
    """
    if not description_text:
        return False
    norm = normalize_text(description_text)
    has_onsite = any(re.search(p, norm) for p in _ONSITE_HYBRID_PATTERNS)
    if not has_onsite:
        return False
    has_remote = any(re.search(p, norm) for p in _REMOTE_PATTERNS)
    return not has_remote


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

# Phrases LinkedIn shows when a posting no longer accepts applications.
# Matched against the accent-stripped, lowercased page text.
_CLOSED_JOB_MARKERS = (
    "no longer accepting applications",
    "nao esta mais aceitando candidaturas",
    "nao aceita mais candidaturas",
    "candidaturas encerradas",
    "vaga encerrada",
)


def job_is_closed(soup):
    """
    Detect whether a LinkedIn guest job page indicates the posting is closed
    (no longer accepting applications), either by the explicit banner/figure or
    by one of the known phrases in the page text.
    """
    if soup.find(class_=re.compile(r"closed-job|jobs-closed")):
        return True
    page_text = normalize_text(soup.get_text(" "))
    return any(marker in page_text for marker in _CLOSED_JOB_MARKERS)


def fetch_job_description(job_id):
    """
    Fetch the detailed job description from the public LinkedIn Guest API endpoint.

    Returns a (description, is_closed) tuple, where is_closed is True when the
    posting no longer accepts applications.
    """
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    headers = get_headers()

    try:
        # Friendly pause before fetching the description to avoid 429
        _sleep_between_requests()

        response = request_with_retry(url, headers=headers)
        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "no response"
            logger.warning(f"Failed to get details for job ID {job_id}. Status: {status}")
            return "Description unavailable due to a public connection error.", False

        soup = BeautifulSoup(response.content, "html.parser")
        is_closed = job_is_closed(soup)

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
            return text, is_closed

        # Fallback: if the known classes are not found, take the whole body text
        body_text = soup.get_text().strip()
        if len(body_text) > 100:
            return body_text, is_closed

        return "Description unavailable in the returned HTML.", is_closed

    except Exception:
        logger.exception(f"Failed to fetch description for job ID {job_id}")
        return "Error extracting the job description.", False

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
    max_pages=None,
):
    """
    Search LinkedIn jobs using the public search Guest API.
    Returns a list of dictionaries with the collected information.

    The geo_id parameter (LinkedIn geoId) is what actually restricts the search
    country; the `location` text parameter alone is not reliably honored by the
    Guest API (it returns US jobs even with location=Brasil).
    """
    if max_pages is None:
        max_pages = scraper_max_pages()

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
            _sleep_between_requests()
            # Serial retry on 429/network error (no parallelism).
            response = request_with_retry(url, headers=headers)

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
                    description, is_closed = fetch_job_description(job_id)

                    # Skip postings that no longer accept applications: no point
                    # spending an LLM classification/match call on them.
                    if is_closed:
                        logger.info(f"Job no longer accepting applications, skipping: {title} | {company} ({job_id})")
                        continue

                    # f_WT only filters at the search level and LinkedIn's remote
                    # filter leaks hybrid/on-site jobs. The card has no work-model
                    # field, so the description is the only per-job signal: in a
                    # remote search, drop jobs that explicitly declare hybrid/
                    # on-site with no remote option.
                    if normalize_text(workplace_type) == "remoto" and description_conflicts_with_remote(description):
                        logger.info(f"Job declared hybrid/on-site in a remote search, skipping: {title} | {company} | {loc}")
                        continue

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

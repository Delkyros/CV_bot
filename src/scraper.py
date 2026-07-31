import time
import random
import re
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup

from src.matcher import classify_contract
from src.text_signals import normalize_text
from src.settings import env_float, env_int, env_list

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


# Defaults preserve the original behavior for the current user (Grande
# Florianopolis hybrid hubs; foreign countries rejected for remote). Both are
# env-overridable so any user/region works — see SCRAPER_HYBRID_HUB_CITIES and
# SCRAPER_REMOTE_REJECTED_COUNTRIES in the README Tunables table.
# "sao jose" is included but gets the homonym guard in workplace_matches (it
# exists in several states; only the Santa Catarina one is a hub).
DEFAULT_HYBRID_HUB_CITIES = ["florianopolis", "floripa", "palhoca", "biguacu", "sao jose"]
DEFAULT_REMOTE_REJECTED_COUNTRIES = [
    "estados unidos", "united states", "canada", "espanha", "spain",
    "portugal", "india", "mexico", "argentina", "reino unido",
    "republica dominicana", "alemanha", "franca",
]


def _hybrid_hub_cities():
    return [normalize_text(c) for c in env_list("SCRAPER_HYBRID_HUB_CITIES", DEFAULT_HYBRID_HUB_CITIES)]


def _remote_rejected_countries():
    return [normalize_text(c) for c in env_list("SCRAPER_REMOTE_REJECTED_COUNTRIES", DEFAULT_REMOTE_REJECTED_COUNTRIES)]


# LinkedIn labels metro areas as "<City> e Região" / "Grande <City>" (seen live:
# "Porto Alegre e Região", "Belo Horizonte e Região"). Stripping the qualifier
# lets those match a hub city while keeping the whole-component check that
# rejects the "São José dos Campos" homonyms.
_METRO_AREA_QUALIFIER = re.compile(r"^(?:grande|regiao metropolitana de)\s+|\s+e\s+regiao$")


def workplace_matches(location_text, workplace_type):
    """
    Confirm the accepted workplace models: remote (rejecting the configured
    foreign countries) or hybrid in the configured hub cities.

    The hub-city list and the remote-rejected-country list come from env
    (SCRAPER_HYBRID_HUB_CITIES / SCRAPER_REMOTE_REJECTED_COUNTRIES); the defaults
    reproduce the original Grande Florianopolis / Brazil behavior.

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
        # The country is already guaranteed by the geoId in the search URL. Here
        # we only reject jobs whose location explicitly mentions a rejected
        # country, without requiring a positive country match (cities like
        # "Campinas, SP" carry no country token).
        return not any(token in location_norm for token in _remote_rejected_countries())

    if normalized_workplace in ("hibrido", "hybrid"):
        # Keep hybrid jobs whose CITY is one of the configured hubs -- NOT a whole
        # state. Match on the comma-delimited location components (City, State,
        # Country) so a hub is matched as a WHOLE city name: "sao jose" matches
        # "São José, SC" but NOT the longer homonyms "São José dos Campos" /
        # "São José do Rio Preto", which are distinct components. No city is
        # special-cased. (Default Grande Florianopolis set rejects other SC cities.)
        # The metro-area qualifier is stripped first so LinkedIn's own
        # "<City> e Região" / "Grande <City>" strings still match the hub.
        cities = set(_hybrid_hub_cities())
        segments = [_METRO_AREA_QUALIFIER.sub("", seg.strip()) for seg in location_norm.split(",")]
        return any(seg in cities for seg in segments)

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


def conflicts_with_remote(text):
    """
    True when `text` EXPLICITLY declares a hybrid/on-site model and gives no sign
    of remote work. Applied to both the job TITLE and the description.

    Exists because LinkedIn's f_WT=2 filter is intermittently ignored and no
    per-job work-model field is reachable without auth, so free text is the only
    signal. Conservative by design: any remote possibility ("remoto ou híbrido"),
    or silence about the model, returns False.

    Evidence and measurements: specs/004-workplace-model-accuracy/research.md.
    """
    onsite, remote = remote_conflict_evidence(text)
    return bool(onsite) and not remote


def remote_conflict_evidence(text):
    """
    (onsite_hits, remote_hits) -- the raw pattern matches behind
    conflicts_with_remote, so callers can tell its two very different False cases
    apart: "no work-model signal at all" vs "on-site signal present but VETOED by
    a remote word". Only the second is a suspect, and collapsing them into one
    bool is why the description guard's effectiveness was unmeasurable.
    """
    if not text:
        return [], []
    norm = normalize_text(text)
    return (
        [p for p in _ONSITE_HYBRID_PATTERNS if re.search(p, norm)],
        [p for p in _REMOTE_PATTERNS if re.search(p, norm)],
    )


def remote_location_shape(job_location, search_location):
    """
    Classify a remote job's location against the searched country. Returns
    "country" | "city_country" | "metro" | "city_state", or None with no input.

    LinkedIn's own Job Posting schema ties location format to work model: Remote
    takes Country / City+Country / Country Cluster, Hybrid/On-site take CITY,STATE.
    So a remote hit narrower than the country is shaped like a hybrid/on-site ad.

    A RANKING signal, never a gate -- 12% precision means a hard drop costs ~7 good
    jobs per bad one. The per-shape error rates are displayed by the web UI
    (web/index.html SHAPE_LABEL) and sourced in
    specs/004-workplace-model-accuracy/research.md.

    The country is the LAST comma component of `search_location`, following
    LinkedIn's "CITY, STATE, COUNTRY" convention, so no country table is needed and
    any configured region works.
    """
    if not job_location or not search_location:
        return None
    country = normalize_text(search_location).split(",")[-1].strip()
    segments = [seg.strip() for seg in normalize_text(job_location).split(",")]
    if segments[-1] == country:
        return "country" if len(segments) == 1 else "city_country"
    if len(segments) == 1 and _METRO_AREA_QUALIFIER.search(segments[0]):
        return "metro"
    return "city_state"


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
    excluded_title_companies=None,
    excluded_companies=None,
    geo_id=None,
    time_filter=None,
    max_pages=None,
    distance=None,
):
    """
    Search LinkedIn jobs using the public search Guest API.
    Returns a list of dictionaries with the collected information.

    The geo_id parameter (LinkedIn geoId) is what actually restricts the search
    country; the `location` text parameter alone is not reliably honored by the
    Guest API (it returns US jobs even with location=Brasil).

    distance is LinkedIn's search radius in MILES around geo_id. Verified against
    the Guest API on a Florianopolis geoId: 0 -> no results, 25/50 -> Florianopolis
    + Sao Jose only, 100 -> Blumenau appears. The implicit default when the
    parameter is omitted was NOT stable across probes, so it is not relied on --
    pass an explicit value when the radius matters.

    It only bites when geo_id is a CITY. On a country-level geoId (the remote
    search) it is a no-op: in a controlled test, fetching the same URL twice
    differed as often as adding distance did (3/6 vs 3/6), i.e. the variation is
    the endpoint's own instability. Left unset when not configured.
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
    excluded_title_companies = set(excluded_title_companies or [])
    excluded_companies = set(excluded_companies or [])

    # LinkedIn Guest search URL
    encoded_keyword = urllib.parse.quote(search_keyword)
    encoded_location = urllib.parse.quote(location)
    workplace_filter = linkedin_workplace_filter(workplace_type)
    tpr_filter = linkedin_time_filter(time_filter)
    # Gates the three work-model checks below; constant for the whole search.
    is_remote_search = normalize_text(workplace_type) == "remoto"

    while len(jobs) < max_jobs and pages < max_pages:
        pages += 1
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={encoded_keyword}&location={encoded_location}&start={start}"
        if geo_id:
            url += f"&geoId={geo_id}"
        if workplace_filter:
            url += f"&f_WT={workplace_filter}"
        if tpr_filter:
            url += f"&f_TPR={tpr_filter}"
        if distance is not None:
            url += f"&distance={distance}"

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

                    # Reposts: LinkedIn re-publishes the same ad under a new ID,
                    # dodging the link check above. If the user already triaged a
                    # job with this exact title+company, skip the repost before
                    # spending a description download (and later an LLM call).
                    if (normalize_text(title), normalize_text(company)) in excluded_title_companies:
                        logger.info(f"Repost of an already-triaged job, skipping: {title} | {company} ({job_id})")
                        continue

                    # Companies that repeatedly advertise hybrid/on-site ads as
                    # remote and that the user has never engaged with (see
                    # main.learned_location_blocklist).
                    if normalize_text(company) in excluded_companies:
                        logger.info(f"Company on the learned location blocklist, skipping: {title} | {company} | {loc}")
                        continue

                    # Filter model/location BEFORE downloading the description: the
                    # location already comes in the card, so we avoid requests (and
                    # the risk of 429) downloading descriptions of jobs that will be
                    # discarded.
                    if not workplace_matches(loc, workplace_type):
                        logger.info(f"Job outside the target model/location, skipping: {title} | {company} | {loc}")
                        continue

                    # Leaked hybrid/on-site ads often spell the model out in the
                    # TITLE ("Engenheiro de IA Pleno | Híbrido| São Paulo/SP"), and
                    # the title is both free (no request) and cleaner than the
                    # description, whose stray "home office" vetoes the check below.
                    if is_remote_search and conflicts_with_remote(title):
                        logger.info(f"Job title declares hybrid/on-site in a remote search, skipping: {title} | {company} | {loc}")
                        continue

                    logger.info(f"Collecting job description: {title} | {company} (ID: {job_id})...")
                    description, is_closed = fetch_job_description(job_id)

                    # Skip postings that no longer accept applications: no point
                    # spending an LLM classification/match call on them.
                    if is_closed:
                        logger.info(f"Job no longer accepting applications, skipping: {title} | {company} ({job_id})")
                        continue

                    # Last resort for the same leak, once the description is in hand
                    # and the title said nothing. Weaker: any stray remote wording in
                    # a long description vetoes it (0 of the 219 flagged jobs were
                    # caught here) -- which is what workplace_evidence below records.
                    if is_remote_search and conflicts_with_remote(description):
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
                        contract_inference = classify_contract(description, title=title, company=company)

                    # Diagnostics for the jobs that survived both gates, i.e. the
                    # ambiguous middle. `onsite > 0 and remote > 0` is the VETOED
                    # case: the leading suspect for a job the user will later flag.
                    title_onsite, title_remote = remote_conflict_evidence(title)
                    desc_onsite, desc_remote = remote_conflict_evidence(description)

                    job_info = {
                        "job_title": title,
                        "company": company,
                        "location": loc,
                        "workplace_type": workplace_type or "N/A",
                        "location_shape": remote_location_shape(loc, location) if is_remote_search else None,
                        "workplace_evidence": {
                            "title_onsite": len(title_onsite),
                            "title_remote": len(title_remote),
                            "desc_onsite": len(desc_onsite),
                            "desc_remote": len(desc_remote),
                        },
                        "contract_type": contract_inference.get("inferred_contract_type", contract_type or "N/A"),
                        "inferred_contract_type": contract_inference.get("inferred_contract_type", "N/A"),
                        "score_clt": contract_inference.get("score_clt", "N/A"),
                        "score_non_clt": contract_inference.get("score_non_clt", "N/A"),
                        "contract_margin": contract_inference.get("contract_margin", "N/A"),
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

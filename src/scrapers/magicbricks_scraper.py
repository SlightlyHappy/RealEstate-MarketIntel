"""
MagicBricks Scraper — Anti-403 Edition
=======================================
Layers of defence against bot detection:

1. TLS/JA3/HTTP2 fingerprinting via curl_cffi — latest Chrome versions
   (the #1 signal Cloudflare uses, missed by plain requests/httpx)
2. Per-chrome-version Sec-CH-UA headers that exactly match the TLS fingerprint
3. Google referrer chain — simulates arriving from a Google search result
4. Residential proxy support — eliminates datacenter-IP detection
   (set PROXY_URL env var, e.g. http://user:pass@gate.smartproxy.com:7000)
5. City-level fresh sessions — new browser identity per city
6. Gaussian inter-request delays — human-like timing distribution
7. Soft-block detection — catches 200 responses that are actually CAPTCHA pages
8. Exponential back-off with fingerprint rotation on hard 403s
"""

import json
import csv
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    import requests as cffi_requests          # fallback — expect more 403s
    CURL_CFFI_AVAILABLE = False

# Proxy URL — set in Railway env vars as PROXY_URL
# Format: http://user:pass@host:port  or  socks5://user:pass@host:port
# Leave unset to scrape without a proxy (datacenter IP, higher 403 rate)
PROXY_URL: Optional[str] = os.getenv("PROXY_URL")

# ─────────────────────────── logging ────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ─────────────────────── Chrome profiles ────────────────────────────────────
# Each entry bundles: curl_cffi impersonate target + matching Sec-CH-UA headers.
# Older entries (chrome120) kept as last-resort fallbacks.
CHROME_PROFILES = [
    {
        "impersonate": "chrome136",
        "sec_ch_ua": '"Google Chrome";v="136", "Chromium";v="136", "Not_A Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "impersonate": "chrome133a",
        "sec_ch_ua": '"Google Chrome";v="133", "Chromium";v="133", "Not_A Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "impersonate": "chrome131",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "impersonate": "chrome124",
        "sec_ch_ua": '"Google Chrome";v="124", "Chromium";v="124", "Not_A Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "impersonate": "chrome120",
        "sec_ch_ua": '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
]

ACCEPT_LANGUAGES = [
    "en-IN,en-GB;q=0.9,en;q=0.8,hi;q=0.5",
    "en-US,en;q=0.9,hi;q=0.7,en-IN;q=0.6",
    "en-GB,en;q=0.9,en-US;q=0.8",
]

# Minimum HTML size (bytes) for a valid listing page
MIN_LISTING_PAGE_SIZE = 50_000

# Keywords that indicate a soft-block / CAPTCHA even on status 200
SOFT_BLOCK_KEYWORDS = [
    "access denied",
    "verify you are human",
    "captcha",
    "cloudflare",
    "enable javascript",
    "checking your browser",
    "just a moment",
    "ddos-guard",
]


# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 scraper class \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
class MagicBricksInfiniteScraper:
    """Scrapes MagicBricks property listings with robust bot-detection evasion."""

    BASE_URL = "https://www.magicbricks.com/property-for-sale/residential-real-estate"

    CITIES = [
        "Delhi-NCR",
        "Bangalore",
        "Mumbai",
        "Hyderabad",
        "Pune",
        "Chennai",
        "Kolkata",
        "Ahmedabad",
        "Jaipur",
        "Indore",
    ]

    BASE_FILTERS = {
        "bedroom": "2,3",
        "proptype": (
            "Multistorey-Apartment,Builder-Floor-Apartment,"
            "Penthouse,Studio-Apartment,Residential-House,Villa"
        ),
    }

    def __init__(self, output_dir: str = "data/raw"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.seen_urls: set = set()
        self.file_lock = Lock()
        self.current_city: str = ""
        self.filters: dict = {}

        # Random Chrome profile for this instance
        self._profile = random.choice(CHROME_PROFILES)
        self.session = self._make_session()
        self._warmed_up = False

        if CURL_CFFI_AVAILABLE:
            logger.info(
                f"[Session] curl_cffi ready \u2014 impersonating {self._profile['impersonate']}"
                + (f" via proxy" if PROXY_URL else " (no proxy \u2014 datacenter IP)")
            )
        else:
            logger.warning("[Session] curl_cffi not available \u2014 higher 403 rate expected")

    # \u2500\u2500 session \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _make_session(self):
        """Create a new curl_cffi session with proxy if configured."""
        if CURL_CFFI_AVAILABLE:
            session = cffi_requests.Session(impersonate=self._profile["impersonate"])
        else:
            session = cffi_requests.Session()
        if PROXY_URL:
            session.proxies = {"https": PROXY_URL, "http": PROXY_URL}
        return session

    def _rotate_profile(self):
        """Switch to a different Chrome profile and open a fresh session."""
        remaining = [p for p in CHROME_PROFILES if p["impersonate"] != self._profile["impersonate"]]
        self._profile = random.choice(remaining) if remaining else random.choice(CHROME_PROFILES)
        logger.info(f"[Session] Rotating fingerprint \u2192 {self._profile['impersonate']}")
        self.session = self._make_session()
        self._warmed_up = False

    def _build_headers(self, referer: str) -> dict:
        """Build a complete, fingerprint-consistent header set."""
        return {
            "Accept-Language": random.choice(ACCEPT_LANGUAGES),
            "Referer": referer,
            "Cache-Control": "max-age=0",
            "Sec-CH-UA": self._profile["sec_ch_ua"],
            "Sec-CH-UA-Mobile": self._profile["sec_ch_ua_mobile"],
            "Sec-CH-UA-Platform": self._profile["sec_ch_ua_platform"],
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _warm_up(self, city: str = ""):
        """
        3-hop warm-up simulating arrival from a Google search result:
          Google search \u2192 MagicBricks homepage \u2192 city listing page
        Builds a real cookie jar and referer chain that bot detectors expect.
        """
        if self._warmed_up:
            return

        city_label = city or self.current_city or "Mumbai"
        search_term = f"magicbricks property for sale {city_label.replace('-', ' ')}"
        google_url = "https://www.google.com/search?q=" + search_term.replace(" ", "+") + "&hl=en-IN"

        try:
            logger.info(f"[Session] 1/3 Google search: '{search_term}'")
            hdrs = {
                "Accept-Language": random.choice(ACCEPT_LANGUAGES),
                "Referer": "https://www.google.com/",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Sec-CH-UA": self._profile["sec_ch_ua"],
                "Sec-CH-UA-Mobile": self._profile["sec_ch_ua_mobile"],
                "Sec-CH-UA-Platform": self._profile["sec_ch_ua_platform"],
            }
            r = self.session.get(google_url, headers=hdrs, timeout=20)
            logger.info(f"[Session] 1/3 Google \u2192 {r.status_code}")
            time.sleep(random.uniform(2.5, 5.0))

            logger.info("[Session] 2/3 Landing on MagicBricks homepage (from Google)...")
            hdrs2 = self._build_headers(referer=google_url)
            hdrs2["Sec-Fetch-Site"] = "cross-site"
            r2 = self.session.get("https://www.magicbricks.com/", headers=hdrs2, timeout=20)
            logger.info(f"[Session] 2/3 Homepage \u2192 {r2.status_code} \u2014 {len(dict(r2.cookies))} cookies")
            time.sleep(random.uniform(3.0, 7.0))

            logger.info(f"[Session] 3/3 Navigating to city search: {city_label}")
            search_url = f"{self.BASE_URL}?cityName={city_label}"
            hdrs3 = self._build_headers(referer="https://www.magicbricks.com/")
            r3 = self.session.get(search_url, headers=hdrs3, timeout=20)
            logger.info(f"[Session] 3/3 City page \u2192 {r3.status_code} \u2014 warm-up complete \u2713")
            time.sleep(random.uniform(3.0, 6.0))

        except Exception as exc:
            logger.warning(f"[Session] Warm-up failed (continuing anyway): {exc}")

        self._warmed_up = True

    # \u2500\u2500 URL helpers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def set_city(self, city: str):
        self.current_city = city
        self.filters = {**self.BASE_FILTERS, "cityName": city}

    def build_url(self, page: int = 1) -> str:
        params = "&".join(f"{k}={v}" for k, v in self.filters.items())
        return f"{self.BASE_URL}?{params}&page={page}"

    # \u2500\u2500 core fetch \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def fetch_page(self, url: str, page_num: int = 1, is_detail: bool = False) -> Optional[str]:
        """
        Fetch a URL with full evasion stack.
        Returns HTML string on success, None on unrecoverable failure.
        """
        MAX_RETRIES = 4
        # Gaussian delay: ~6s for listing pages, ~4s for detail pages
        base_mu    = 4.0 if is_detail else 6.0
        base_sigma = 1.5

        self._warm_up(self.current_city)

        referer = (
            self.build_url(max(1, page_num - 1)) if not is_detail
            else self.BASE_URL + f"?cityName={self.current_city}"
        )

        for attempt in range(1, MAX_RETRIES + 1):
            delay = max(2.0, random.gauss(base_mu, base_sigma))
            if attempt > 1:
                delay += random.uniform(10.0, 20.0) * (attempt - 1)
            time.sleep(delay)

            try:
                headers = self._build_headers(referer=referer)
                if is_detail:
                    headers["Sec-Fetch-Site"] = "same-origin"

                label = "Detail" if is_detail else "Page"
                logger.info(f"[{label} {page_num}] Attempt {attempt}/{MAX_RETRIES}: {url[:80]}...")

                resp = self.session.get(url, headers=headers, timeout=30, allow_redirects=True)

                if resp.status_code in (403, 429):
                    backoff = random.uniform(25.0, 55.0) * attempt
                    logger.warning(
                        f"[{label} {page_num}] {resp.status_code} (attempt {attempt}) \u2014 "
                        f"rotating fingerprint + back-off {backoff:.0f}s"
                    )
                    if attempt < MAX_RETRIES:
                        time.sleep(backoff)
                        self._rotate_profile()
                        self._warm_up(self.current_city)
                    else:
                        logger.error(f"[{label} {page_num}] Giving up after {MAX_RETRIES} attempts")
                        return None
                    continue

                if resp.status_code != 200:
                    logger.error(f"[{label} {page_num}] HTTP {resp.status_code} \u2014 aborting")
                    return None

                html = resp.text

                # Soft-block: response too small for a real listing page
                if not is_detail and len(html) < MIN_LISTING_PAGE_SIZE:
                    logger.warning(f"[{label} {page_num}] Too small ({len(html):,} chars) \u2014 soft-block?")
                    if attempt < MAX_RETRIES:
                        self._rotate_profile()
                        self._warm_up(self.current_city)
                        continue

                # Soft-block: CAPTCHA keywords in body
                lower = html.lower()
                blocked = next((kw for kw in SOFT_BLOCK_KEYWORDS if kw in lower), None)
                if blocked:
                    logger.warning(f"[{label} {page_num}] Soft-block keyword '{blocked}' \u2014 rotating")
                    if attempt < MAX_RETRIES:
                        time.sleep(random.uniform(20.0, 40.0))
                        self._rotate_profile()
                        self._warm_up(self.current_city)
                    continue

                logger.info(f"[{label} {page_num}] \u2713 200 OK \u2014 {len(html):,} chars")
                referer = url
                return html

            except Exception as exc:
                logger.warning(f"[Page {page_num}] Request error (attempt {attempt}): {exc}")
                time.sleep(random.uniform(8.0, 16.0))

        return None
    
    # \u2500\u2500 parsing \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def extract_property_listings(self, html: str, page_num: int = 1) -> List[Dict]:
        """Extract property listing cards from a search-results page."""
        properties = []
        try:
            soup = BeautifulSoup(html, "html.parser")
            all_links = soup.find_all("a", href=re.compile(r"propertyDetails|/property/", re.I))
            logger.info(f"[Page {page_num}] Found {len(all_links)} property links")

            for link in all_links[:100]:
                try:
                    url = link.get("href", "").strip()
                    if not url:
                        continue
                    if not url.startswith("http"):
                        url = "https://www.magicbricks.com" + url
                    if url in self.seen_urls:
                        continue
                    self.seen_urls.add(url)

                    prop: Dict = {"url": url}
                    slug = url.split("/")[-1] if "/" in url else url

                    m = re.search(r"(\d+)-BHK", slug, re.I)
                    if m:
                        prop["bhk"] = m.group(1)

                    m = re.search(r"(\d+)-Sq-(?:ft|yrd)", slug, re.I)
                    if m:
                        prop["area_sqft"] = m.group(1)

                    for pt in [
                        "Multistorey-Apartment", "Builder-Floor-Apartment",
                        "Residential-House", "Villa", "Penthouse", "Studio-Apartment",
                    ]:
                        if pt in slug:
                            prop["property_type"] = pt.replace("-", " ")
                            break

                    m = re.search(r"in-([A-Za-z-]+)&id", slug)
                    if m:
                        prop["location"] = m.group(1).replace("-", " ")

                    parts = []
                    if "bhk" in prop:       parts.append(f"{prop['bhk']} BHK")
                    if "property_type" in prop: parts.append(prop["property_type"])
                    if "location" in prop:  parts.append(f"in {prop['location']}")
                    prop["title"] = " ".join(parts) or "Property Listing"
                    prop["scraped_at"] = datetime.now().isoformat()
                    properties.append(prop)

                except Exception:
                    continue

            logger.info(f"[Page {page_num}] Extracted {len(properties)} new properties")

        except Exception as exc:
            logger.error(f"[Page {page_num}] Parse error: {exc}")

        return properties

    def extract_property_detail(self, html: str) -> Dict:
        """Extract price / area / BHK from a property detail page."""
        detail: Dict = {}
        try:
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text()

            m = re.search(r"\u20b9\s*([\d.,]+)\s*(?:Cr|Crore|Lac)", text)
            if m:
                detail["price"] = m.group(1).replace(",", "")
                um = re.search(r"\u20b9\s*[\d.,]+\s*(Cr|Crore|Lac)", text)
                if um:
                    detail["price_unit"] = um.group(1)

            m = re.search(r"(\d+)\s*BHK", text)
            if m:
                detail["bhk"] = m.group(1)

            m = re.search(r"(\d+[.,]*\d*)\s*(?:Sq\.?\s*ft|sqft|sq\.ft)", text, re.I)
            if m:
                detail["area_sqft"] = m.group(1).replace(",", "")

            for pt in ["Apartment", "Villa", "House", "Penthouse", "Studio"]:
                if pt.lower() in text.lower():
                    detail["property_type"] = pt
                    break

            h1 = soup.find("h1")
            if h1:
                detail["title"] = h1.get_text(strip=True)

            detail["scraped_at"] = datetime.now().isoformat()

        except Exception as exc:
            logger.error(f"Detail parse error: {exc}")

        return detail

    # \u2500\u2500 file I/O \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def append_property_jsonl(self, data: Dict, filename: str = "properties.jsonl"):
        try:
            filepath = self.output_dir / filename
            with self.file_lock:
                with open(filepath, "a", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                    f.write("\n")
        except Exception as exc:
            logger.error(f"JSONL write error: {exc}")

    def append_property_csv(self, data: Dict, filename: str = "properties.csv"):
        try:
            filepath = self.output_dir / filename
            exists = filepath.exists() and filepath.stat().st_size > 0
            with self.file_lock:
                with open(filepath, "a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=sorted(data.keys()))
                    if not exists:
                        writer.writeheader()
                    writer.writerow(data)
        except Exception as exc:
            logger.error(f"CSV write error: {exc}")


# ── per-city task ────────────────────────────────────────────────────────────────

def scrape_single_city_task(
    city: str,
    max_pages: int = 50,
    enable_details: bool = True,
    output_dir: str = None,
) -> Dict[str, int]:
    """Scrape one city end-to-end. Each call creates a fresh scraper instance."""
    try:
        effective_dir = output_dir or os.getenv("DATA_DIR") or str(
            Path(__file__).parent.parent.parent / "data" / "raw"
        )
        scraper = MagicBricksInfiniteScraper(output_dir=effective_dir)
        scraper.set_city(city)

        city_props: List[Dict] = []

        # Phase 1 — listing pages
        for page in range(1, max_pages + 1):
            url = scraper.build_url(page=page)
            html = scraper.fetch_page(url, page_num=page)
            if not html:
                logger.warning(f"[{city}] Page {page}: fetch failed — stopping city.")
                break
            listings = scraper.extract_property_listings(html, page_num=page)
            city_props.extend(listings)
            if not listings:
                logger.info(f"[{city}] Page {page}: no new listings — done.")
                break

        logger.info(f"[{city}] Listing phase complete: {len(city_props)} properties")

        # Phase 2 — detail enrichment + save
        saved = 0
        for idx, prop in enumerate(city_props, 1):
            try:
                if enable_details:
                    detail_html = scraper.fetch_page(prop["url"], page_num=idx, is_detail=True)
                    merged = {**prop, **(scraper.extract_property_detail(detail_html) if detail_html else {})}
                else:
                    merged = prop
            except Exception as exc:
                logger.warning(f"[{city}] Detail error #{idx}: {exc}")
                merged = prop

            scraper.append_property_jsonl(merged, "magicbricks_all_cities.jsonl")
            scraper.append_property_csv(merged, "magicbricks_all_cities.csv")
            saved += 1
            if saved % 10 == 0:
                logger.info(f"[{city}] Saved {saved}/{len(city_props)} so far …")

        logger.info(f"[{city}] Done — {saved} properties written.")
        return {"city": city, "total": saved}

    except Exception as exc:
        logger.error(f"[{city}] Fatal error: {exc}", exc_info=True)
        return {"city": city, "total": 0}


# ── main entry point ──────────────────────────────────────────────────────────────

def scrape_infinite_parallel(
    max_pages: int = 50,
    enable_details: bool = True,
    max_workers: int = 1,
):
    """Scrape all cities, sequentially by default (max_workers=1).

    Running cities one-at-a-time dramatically reduces 403 risk on datacenter IPs.
    Set max_workers=2 only if a residential proxy is configured via PROXY_URL.
    """
    DATA_DIR = os.getenv("DATA_DIR") or str(
        Path(__file__).parent.parent.parent / "data" / "raw"
    )
    data_path = Path(DATA_DIR)
    data_path.mkdir(parents=True, exist_ok=True)

    jsonl_file = data_path / "magicbricks_all_cities.jsonl"
    csv_file   = data_path / "magicbricks_all_cities.csv"

    # Clear previous run
    for f in (jsonl_file, csv_file):
        if f.exists():
            f.unlink()

    cities = MagicBricksInfiniteScraper.CITIES
    logger.info("=" * 70)
    logger.info("MAGICBRICKS SCRAPER — PARALLEL MULTI-CITY MODE")
    logger.info(f"  Cities      : {len(cities)}")
    logger.info(f"  Max workers : {max_workers}")
    logger.info(f"  Pages/city  : {max_pages}")
    logger.info(f"  Details     : {enable_details}")
    logger.info(f"  Proxy       : {'YES (' + PROXY_URL + ')' if PROXY_URL else 'NO (datacenter IP)'}")
    logger.info(f"  Output dir  : {DATA_DIR}")
    logger.info("=" * 70)

    total = 0
    city_results: List[Dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for idx, city in enumerate(cities):
            # Stagger submissions so threads don't all warm-up simultaneously
            if idx > 0:
                pause = random.uniform(30.0, 75.0)
                logger.info(f"Inter-city pause: {pause:.0f}s before {city} …")
                time.sleep(pause)
            fut = executor.submit(scrape_single_city_task, city, max_pages, enable_details, DATA_DIR)
            futures[fut] = city

        for fut in as_completed(futures):
            city = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:
                logger.error(f"[{city}] Unhandled exception: {exc}", exc_info=True)
                result = {"city": city, "total": 0}
            city_results.append(result)
            total += result["total"]
            logger.info(f"[Progress] {city}: {result['total']} saved  |  running total: {total}")

    # Final summary
    logger.info("=" * 70)
    logger.info("SCRAPE COMPLETE")
    logger.info(f"  Total properties : {total}")
    for r in sorted(city_results, key=lambda x: x["total"], reverse=True):
        logger.info(f"    {r['city']:20s} {r['total']:>5d}")
    if jsonl_file.exists():
        logger.info(f"  JSONL size : {jsonl_file.stat().st_size / 1024:.1f} KB")
    if csv_file.exists():
        logger.info(f"  CSV  size  : {csv_file.stat().st_size / 1024:.1f} KB")
    logger.info("=" * 70)

    return total


if __name__ == "__main__":
    scrape_infinite_parallel(max_pages=50, enable_details=True, max_workers=1)

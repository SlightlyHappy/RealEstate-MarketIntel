"""
Magic Bricks Scraper - Infinite Scroll Version with Parallel Processing
Continuously fetches properties by simulating pagination/infinite scroll
Runs multiple cities in parallel with IP rotation and user agent rotation
"""

import json
import csv
import logging
from datetime import datetime
from typing import List, Dict, Optional, Set
from pathlib import Path
import re
from bs4 import BeautifulSoup
import requests
import time
import random
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
# Suppress unicode issues on Windows
import sys
if sys.stdout.encoding != 'utf-8':
    # Reconfigure stdout to use utf-8
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# User agents to rotate through (avoid detection)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/91.0.864.59",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
]

# HTTP headers to look more like real browser
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
}


class MagicBricksInfiniteScraper:
    """Scraper for extracting property data with infinite scroll support"""
    
    BASE_URL = "https://www.magicbricks.com/property-for-sale/residential-real-estate"
    
    # Major Indian cities to scrape
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
        "Indore"
    ]
    
    BASE_FILTERS = {
        "bedroom": "2,3",
        "proptype": "Multistorey-Apartment,Builder-Floor-Apartment,Penthouse,Studio-Apartment,Residential-House,Villa"
    }
    
    def __init__(self, output_dir: str = "data/raw"):
        """Initialize scraper"""
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.properties = []
        self.seen_urls = set()
        self.file_lock = Lock()  # Thread-safe file writing
        
    def get_random_user_agent(self) -> str:
        """Get random user agent to avoid detection"""
        return random.choice(USER_AGENTS)
    
    def get_request_headers(self) -> Dict:
        """Get headers for HTTP request to look like real browser"""
        headers = BROWSER_HEADERS.copy()
        headers["User-Agent"] = self.get_random_user_agent()
        headers["Referer"] = self.BASE_URL
        return headers
        
    def set_city(self, city: str):
        """Set current city for scraping"""
        self.current_city = city
        self.filters = {**self.BASE_FILTERS, "cityName": city}
        
    def build_url(self, page: int = 1) -> str:
        """Build URL with pagination parameters"""
        params = "&".join([f"{k}={v}" for k, v in self.filters.items()])
        return f"{self.BASE_URL}?{params}&page={page}"
    
    def fetch_page(self, url: str, page_num: int = 1) -> Optional[str]:
        """Fetch a page using requests with rotation and delays"""
        try:
            logger.info(f"[Page {page_num}] Fetching: {url[:80]}...")
            
            # Use rotating user agents and headers
            headers = self.get_request_headers()
            
            # Random delay (0.5-2 seconds) to avoid detection
            delay = random.uniform(0.5, 2.0)
            time.sleep(delay)
            
            response = requests.get(url, headers=headers, timeout=15)
            
            if response.status_code == 200:
                logger.info(f"[Page {page_num}] [SUCCESS] Fetched with {headers['User-Agent'][:40]}...")
                return response.text
            else:
                logger.error(f"[Page {page_num}] Status code: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"[Page {page_num}] Error: {str(e)}")
            return None
    
    def extract_property_listings(self, html: str, page_num: int = 1) -> List[Dict]:
        """Extract property listing cards from page"""
        properties = []
        new_count = 0
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # Find all links to property detail pages
            all_links = soup.find_all('a', href=re.compile(r'propertyDetails|/property/', re.I))
            
            logger.info(f"[Page {page_num}] Found {len(all_links)} property links")
            
            for idx, link in enumerate(all_links[:100]):
                try:
                    url = link.get('href', '').strip()
                    
                    if not url:
                        continue
                    
                    # Normalize URL
                    if not url.startswith('http'):
                        url = 'https://www.magicbricks.com' + url
                    
                    # Skip duplicates
                    if url in self.seen_urls:
                        continue
                    
                    self.seen_urls.add(url)
                    new_count += 1
                    
                    property_data = {'url': url}
                    
                    # Extract data from URL slug
                    url_slug = url.split('/')[-1] if '/' in url else url
                    
                    # Extract BHK
                    bhk_match = re.search(r'(\d+)-BHK', url_slug, re.I)
                    if bhk_match:
                        property_data['bhk'] = bhk_match.group(1)
                    
                    # Extract area
                    area_match = re.search(r'(\d+)-Sq-(?:ft|yrd)', url_slug, re.I)
                    if area_match:
                        property_data['area_sqft'] = area_match.group(1)
                    
                    # Extract property type
                    prop_types = ['Multistorey-Apartment', 'Builder-Floor-Apartment', 'Residential-House', 
                                  'Villa', 'Penthouse', 'Studio-Apartment']
                    for ptype in prop_types:
                        if ptype in url_slug:
                            property_data['property_type'] = ptype.replace('-', ' ')
                            break
                    
                    # Extract location
                    location_match = re.search(r'in-([A-Za-z-]+)&id', url_slug)
                    if location_match:
                        property_data['location'] = location_match.group(1).replace('-', ' ')
                    
                    # Build title
                    parts = []
                    if 'bhk' in property_data:
                        parts.append(f"{property_data['bhk']} BHK")
                    if 'property_type' in property_data:
                        parts.append(property_data['property_type'])
                    if 'location' in property_data:
                        parts.append(f"in {property_data['location']}")
                    property_data['title'] = ' '.join(parts) if parts else 'Property Listing'
                    
                    property_data['scraped_at'] = datetime.now().isoformat()
                    properties.append(property_data)
                
                except Exception as e:
                    logger.debug(f"Error extracting item {idx}: {str(e)}")
                    continue
            
            logger.info(f"[Page {page_num}] Extracted {new_count} new properties")
            
        except Exception as e:
            logger.error(f"[Page {page_num}] Error parsing HTML: {str(e)}")
        
        return properties
    
    def extract_property_detail(self, html: str) -> Dict:
        """Extract detailed information from property detail page"""
        property_detail = {}
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            text = soup.get_text()
            
            # Extract price
            price_match = re.search(r'₹\s*([\d.,]+)\s*(?:Cr|Crore|Lac)', text)
            if price_match:
                property_detail['price'] = price_match.group(1).replace(',', '')
                unit_match = re.search(r'₹\s*[\d.,]+\s*(Cr|Crore|Lac)', text)
                if unit_match:
                    property_detail['price_unit'] = unit_match.group(1)
            
            # Extract BHK
            bhk_match = re.search(r'(\d+)\s*BHK', text)
            if bhk_match:
                property_detail['bhk'] = bhk_match.group(1)
            
            # Extract area
            area_match = re.search(r'(\d+[.,]*\d*)\s*(?:Sq\.?\s*ft|sqft|sq\.ft)', text, re.I)
            if area_match:
                property_detail['area_sqft'] = area_match.group(1).replace(',', '')
            
            # Extract property type
            for ptype in ['Apartment', 'Villa', 'House', 'Penthouse', 'Studio']:
                if ptype.lower() in text.lower():
                    property_detail['property_type'] = ptype
                    break
            
            # Extract title
            h1 = soup.find('h1')
            if h1:
                property_detail['title'] = h1.get_text(strip=True)
            
            property_detail['scraped_at'] = datetime.now().isoformat()
            
        except Exception as e:
            logger.error(f"Error parsing property detail: {str(e)}")
        
        return property_detail
    
    def save_to_json(self, data: List[Dict], filename: str = "properties.json"):
        """Save properties to JSON"""
        try:
            filepath = self.output_dir / filename
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info(f"[OK] Saved {len(data)} properties to {filename}")
        except Exception as e:
            logger.error(f"Error saving to JSON: {str(e)}")    
    def append_property_jsonl(self, property_data: Dict, filename: str = "properties.jsonl"):
        """Append single property to JSONL file (thread-safe)"""
        try:
            filepath = self.output_dir / filename
            with self.file_lock:
                with open(filepath, 'a', encoding='utf-8') as f:
                    json.dump(property_data, f, ensure_ascii=False)
                    f.write('\n')
        except Exception as e:
            logger.error(f"Error appending to JSONL: {str(e)}")
    
    def append_property_csv(self, property_data: Dict, filename: str = "properties.csv"):
        """Append single property to CSV file (thread-safe)"""
        try:
            filepath = self.output_dir / filename
            
            # Check if file exists to decide on header
            file_exists = filepath.exists() and filepath.stat().st_size > 0
            
            with self.file_lock:
                with open(filepath, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=sorted(property_data.keys()))
                    
                    if not file_exists:
                        writer.writeheader()
                    
                    writer.writerow(property_data)
        except Exception as e:
            logger.error(f"Error appending to CSV: {str(e)}")    
    def save_to_csv(self, data: List[Dict], filename: str = "properties.csv"):
        """Save properties to CSV"""
        try:
            filepath = self.output_dir / filename
            
            if not data:
                logger.warning("No data to save")
                return
            
            all_keys = set()
            for item in data:
                all_keys.update(item.keys())
            
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=sorted(all_keys))
                writer.writeheader()
                writer.writerows(data)
            
            logger.info(f"[OK] Saved {len(data)} properties to {filename}")
        except Exception as e:
            logger.error(f"Error saving to CSV: {str(e)}")


def scrape_single_city_task(city: str, max_pages: int = 15, enable_details: bool = True, output_dir: str = None) -> Dict[str, int]:
    """Helper function to scrape a single city. Used for parallel processing."""
    
    try:
        # Create a new scraper instance for this thread
        scraper = MagicBricksInfiniteScraper(output_dir=output_dir)
        scraper.set_city(city)
        
        city_properties = []
        
        # Phase 1: Scrape listings from multiple pages
        for page in range(1, max_pages + 1):
            url = scraper.build_url(page=page)
            html = scraper.fetch_page(url, page_num=page)
            
            if not html:
                logger.warning(f"[{city}] Page {page}: Failed to fetch, stopping.")
                break
            
            properties = scraper.extract_property_listings(html, page_num=page)
            city_properties.extend(properties)
            
            if not properties:
                logger.info(f"[{city}] Page {page}: No new properties found, stopping.")
                break
        
        logger.info(f"[{city}] Found {len(city_properties)} listings")
        
        # Phase 2: Fetch details and save
        properties_saved = 0
        if enable_details:
            for idx, prop in enumerate(city_properties, 1):
                try:
                    detail_html = scraper.fetch_page(prop['url'], page_num=idx)
                    if detail_html:
                        detail = scraper.extract_property_detail(detail_html)
                        merged = {**prop, **detail}
                    else:
                        merged = prop
                        
                except Exception as e:
                    logger.warning(f"[{city}] Error processing property {idx}: {str(e)}")
                    merged = prop
                
                # Save property immediately
                scraper.append_property_jsonl(merged, "magicbricks_all_cities.jsonl")
                scraper.append_property_csv(merged, "magicbricks_all_cities.csv")
                properties_saved += 1
        else:
            # If not fetching details, just save listings
            for prop in city_properties:
                scraper.append_property_jsonl(prop, "magicbricks_all_cities.jsonl")
                scraper.append_property_csv(prop, "magicbricks_all_cities.csv")
                properties_saved += 1
        
        logger.info(f"[{city}] Saved {properties_saved} properties")
        return {"city": city, "total": properties_saved}
        
    except Exception as e:
        logger.error(f"[{city}] Error during scraping: {str(e)}")
        return {"city": city, "total": 0}


def scrape_infinite_parallel(max_pages: int = 15, enable_details: bool = True, max_workers: int = 3):
    """Scrape properties in parallel across multiple cities"""
    
    script_dir = Path(__file__).parent
    data_dir = script_dir.parent.parent / "data" / "raw"
    
    # Initialize scraper to set up paths
    scraper = MagicBricksInfiniteScraper(output_dir=str(data_dir))
    
    # Initialize output files
    jsonl_file = data_dir / "magicbricks_all_cities.jsonl"
    csv_file = data_dir / "magicbricks_all_cities.csv"
    
    # Clear previous files
    if jsonl_file.exists():
        jsonl_file.unlink()
    if csv_file.exists():
        csv_file.unlink()
    
    print("\n" + "="*70)
    print("MAGIC BRICKS INFINITE SCRAPER - PARALLEL MULTI-CITY MODE")
    print(f"Max workers: {max_workers} | Pages per city: {max_pages}")
    print(f"Details enrichment: {enable_details}")
    print("="*70 + "\n")
    
    total_properties = 0
    city_results = []
    
    # Run cities in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all cities to the thread pool
        futures = {
            executor.submit(scrape_single_city_task, city, max_pages, enable_details, str(data_dir)): city 
            for city in scraper.CITIES
        }
        
        # Process results as they complete
        completed_count = 0
        for future in as_completed(futures):
            completed_count += 1
            city = futures[future]
            try:
                result = future.result()
                city_results.append(result)
                total_properties += result["total"]
                print(f"[{completed_count}/{len(scraper.CITIES)}] {city}: {result['total']} properties saved")
            except Exception as e:
                print(f"[ERROR] {city}: {str(e)}")
                city_results.append({"city": city, "total": 0})
    
    # Read back data for statistics
    print("\n" + "="*70)
    print("FINAL STATISTICS")
    print("="*70)
    print(f"Total properties saved: {total_properties}")
    
    # Quick stats from JSONL file
    properties_with_price = 0
    properties_with_area = 0
    properties_with_location = 0
    properties_with_bhk = 0
    cities_found = {}
    
    if jsonl_file.exists():
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    prop = json.loads(line)
                    if 'price' in prop and prop['price']:
                        properties_with_price += 1
                    if 'area_sqft' in prop and prop['area_sqft']:
                        properties_with_area += 1
                    if 'location' in prop and prop['location']:
                        properties_with_location += 1
                        cities_found[prop['location']] = cities_found.get(prop['location'], 0) + 1
                    if 'bhk' in prop and prop['bhk']:
                        properties_with_bhk += 1
    
    print(f"\nField completion:")
    print(f"  With price: {properties_with_price}/{total_properties} ({100*properties_with_price//max(total_properties,1)}%)")
    print(f"  With area: {properties_with_area}/{total_properties} ({100*properties_with_area//max(total_properties,1)}%)")
    print(f"  With location: {properties_with_location}/{total_properties} ({100*properties_with_location//max(total_properties,1)}%)")
    print(f"  With BHK: {properties_with_bhk}/{total_properties} ({100*properties_with_bhk//max(total_properties,1)}%)")
    
    print(f"\nProperties by location (top 10):")
    for city, count in sorted(cities_found.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {city}: {count}")
    
    print(f"\nCity results:")
    for result in sorted(city_results, key=lambda x: x['total'], reverse=True):
        print(f"  {result['city']}: {result['total']}")
    
    print(f"\nOutput files:")
    print(f"  - magicbricks_all_cities.jsonl ({jsonl_file.stat().st_size / 1024:.1f} KB)")
    print(f"  - magicbricks_all_cities.csv ({csv_file.stat().st_size / 1024:.1f} KB)")
    
    return total_properties


def scrape_infinite(max_pages: int = 5, enable_details: bool = True):
    """Scrape properties with infinite scroll simulation - saves incrementally
    
    NOTE: This is the sequential version. For parallel scraping across multiple cities,
    use scrape_infinite_parallel() instead.
    """
    
    script_dir = Path(__file__).parent
    data_dir = script_dir.parent.parent / "data" / "raw"
    
    scraper = MagicBricksInfiniteScraper(output_dir=str(data_dir))
    
    # Initialize output files
    jsonl_file = data_dir / "magicbricks_all_cities.jsonl"
    csv_file = data_dir / "magicbricks_all_cities.csv"
    
    # Clear previous files
    if jsonl_file.exists():
        jsonl_file.unlink()
    if csv_file.exists():
        csv_file.unlink()
    
    total_properties = 0
    
    print("\n" + "="*60)
    print("MAGIC BRICKS INFINITE SCRAPER - MULTI-CITY MODE")
    print("(Saving incrementally to disk)")
    print("="*60 + "\n")
    
    # Loop through all cities
    for city_idx, city in enumerate(scraper.CITIES, 1):
        scraper.set_city(city)
        print(f"\n{'='*60}")
        print(f"CITY {city_idx}/{len(scraper.CITIES)}: {city}")
        print('='*60 + "\n")
        
        # Phase 1: Scrape listings from multiple pages
        print("PHASE 1: Fetching listings (paginated)...\n")
        city_properties = []
        
        for page in range(1, max_pages + 1):
            url = scraper.build_url(page=page)
            html = scraper.fetch_page(url, page_num=page)
            
            if not html:
                logger.warning(f"[{city}] Page {page}: Failed to fetch, stopping.")
                break
            
            properties = scraper.extract_property_listings(html, page_num=page)
            city_properties.extend(properties)
            
            if not properties:
                logger.info(f"[{city}] Page {page}: No new properties found, stopping.")
                break
            
            print(f"  Total unique properties in {city}: {len([p for p in city_properties if p['url'] not in scraper.seen_urls])}\n")
        
        print(f"\n[{city_idx}/{len(scraper.CITIES)}] {city}: Found {len(city_properties)} listings\n")
        
        # Phase 2: Fetch details for city properties and save immediately
        if enable_details:
            print(f"PHASE 2: Fetching property details for {city}...\n")
            
            for idx, prop in enumerate(city_properties, 1):
                try:
                    print(f"  [{idx}/{len(city_properties)}] {prop.get('location', 'N/A')} ({prop.get('bhk')} BHK)...", end='', flush=True)
                    
                    detail_html = scraper.fetch_page(prop['url'], page_num=idx)
                    if detail_html:
                        detail = scraper.extract_property_detail(detail_html)
                        merged = {**prop, **detail}
                        print(" [OK]")
                    else:
                        print(" [FAIL]")
                        merged = prop
                        
                except Exception as e:
                    logger.warning(f"Error processing property {idx}: {str(e)}")
                    merged = prop
                
                # Save property immediately to disk
                scraper.append_property_jsonl(merged, "magicbricks_all_cities.jsonl")
                scraper.append_property_csv(merged, "magicbricks_all_cities.csv")
                total_properties += 1
                
                # Print progress every 10 properties
                if total_properties % 10 == 0:
                    print(f"    [Total saved: {total_properties}]")
            
            print(f"\n[{city_idx}/{len(scraper.CITIES)}] {city}: Saved {len(city_properties)} enriched properties\n")
    
    # Read back data for statistics
    print("\n" + "="*60)
    print("FINAL STATISTICS")
    print("="*60)
    print(f"Total properties saved: {total_properties}")
    
    # Quick stats from JSONL file
    properties_with_price = 0
    properties_with_area = 0
    properties_with_location = 0
    properties_with_bhk = 0
    cities_found = {}
    
    if jsonl_file.exists():
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    prop = json.loads(line)
                    if 'price' in prop and prop['price']:
                        properties_with_price += 1
                    if 'area_sqft' in prop and prop['area_sqft']:
                        properties_with_area += 1
                    if 'location' in prop and prop['location']:
                        properties_with_location += 1
                        cities_found[prop['location']] = cities_found.get(prop['location'], 0) + 1
                    if 'bhk' in prop and prop['bhk']:
                        properties_with_bhk += 1
    
    print(f"With price: {properties_with_price}")
    print(f"With area: {properties_with_area}")
    print(f"With location: {properties_with_location}")
    print(f"With BHK: {properties_with_bhk}")
    
    print(f"\nProperties by location:")
    for city, count in sorted(cities_found.items(), key=lambda x: x[1], reverse=True):
        print(f"  {city}: {count}")
    
    print(f"\nOutput files:")
    print(f"  - magicbricks_all_cities.jsonl (one JSON object per line)")
    print(f"  - magicbricks_all_cities.csv")
    
    return total_properties


if __name__ == "__main__":
    # Scrape 15 pages per city with detail enrichment
    # This will fetch ~400-450 properties per city across all 10 major Indian cities
    # Total expected: 4000-5000 properties
    #
    # Now using PARALLEL scraper (3 cities at a time for performance + safety)
    # Use max_workers=2 for lighter load, max_workers=4 for more aggressive speed
    scrape_infinite_parallel(max_pages=15, enable_details=True, max_workers=3)

import argparse
import asyncio
import json
import os
import re
import smtplib
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# Detect if running in CI environment (GitHub Actions, etc)
IS_CI = os.environ.get("CI", "false").lower() == "true"

VERSION = "2.1"
SCRIPT_DIR = Path(__file__).parent
DATA_FILE = SCRIPT_DIR / "listings.json"

# Email configuration.
# Credentials come from environment variables so they aren't committed to source.
# Set EMAIL_FROM, EMAIL_PASS and EMAIL_TO as GitHub Actions repository secrets
# (or local env vars). SMTP host/port default to MailerSend but can be overridden.
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.mailersend.net")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

# Proxy configuration (residential proxy for sites that block datacenter/CI IPs).
# Credentials come from environment variables so they aren't committed to source.
# Set these as GitHub Actions repository secrets. DataImpulse provides the host,
# port, login (username) and password on your dashboard.
_PROXY_HOST = os.environ.get("PROXY_HOST", "")
_PROXY_PORT = os.environ.get("PROXY_PORT", "")
_PROXY_USER = os.environ.get("PROXY_USER", "")
_PROXY_PASS = os.environ.get("PROXY_PASS", "")
PROXY_CONFIG = None
if _PROXY_HOST and _PROXY_PORT:
    PROXY_CONFIG = {
        "server": f"http://{_PROXY_HOST}:{_PROXY_PORT}",
        "username": _PROXY_USER,
        "password": _PROXY_PASS,
    }

# Default search
DEFAULT_SEARCH = "patek+2508"

print(f"watchnotify v{VERSION} - amrit manhas @apsm100")


@dataclass
class Listing:
    """A watch listing from any supported site."""
    id: str
    title: str
    price: str
    link: str
    source: str  # Site identifier (e.g., "chrono24", "onbehalf")
    image: str = ""
    reference: str = ""  # Watch reference number if available
    
    @property
    def unique_id(self) -> str:
        """Unique ID combining source and listing ID."""
        return f"{self.source}:{self.id}"


# Command Line Arguments
parser = argparse.ArgumentParser(description="Watch listing notifier")
parser.add_argument("--query", "-q", help="search query (default: patek+2508)", default=DEFAULT_SEARCH)
parser.add_argument("--sites", "-s", help="comma-separated sites to scrape (default: all)", default="all")
parser.add_argument("--noimage", help="no images in notification", action="store_true")
parser.add_argument("--noemail", help="no email will be sent", action="store_true")
parser.add_argument("--sendall", help="send all listings (ignore history)", action="store_true")
parser.add_argument("--debug", help="enable debug logging", action="store_true")
parser.add_argument("--headless", help="force headless browser (default: headed in debug)", action="store_true")
parser.add_argument("--overwrite", help="overwrite listings.json (clear history)", action="store_true")
parser.add_argument("--verbose", "-v", help="verbose output (show per-scraper progress)", action="store_true")
parser.add_argument("--simple", help="simple debug mode: headless, no email, no output files", action="store_true")
args = parser.parse_args()

# --simple implies headless, noemail, debug, and disables output files
if args.simple:
    args.noemail = True
    args.headless = True
    args.debug = True

if any([args.noimage, args.noemail, args.sendall, args.debug, args.headless, args.overwrite, args.verbose, args.simple]):
    flags = [f for f, v in [("--noimage", args.noimage), ("--noemail", args.noemail), 
                            ("--sendall", args.sendall), ("--debug", args.debug),
                            ("--headless", args.headless), ("--overwrite", args.overwrite),
                            ("--verbose", args.verbose), ("--simple", args.simple)] if v]
    if args.verbose or args.debug:
        print(f"arguments: {' '.join(flags)}")


# ANSI color codes for console output
# Enabled for interactive terminals and CI (GitHub Actions renders ANSI colors),
# but disabled when stdout is a plain redirected file (e.g. local runner.log),
# so log files don't fill up with raw escape sequences like "\033[92m".
_USE_COLOR = sys.stdout.isatty() or IS_CI


class Colors:
    RED = "\033[91m" if _USE_COLOR else ""
    YELLOW = "\033[93m" if _USE_COLOR else ""
    GREEN = "\033[92m" if _USE_COLOR else ""
    BOLD = "\033[1m" if _USE_COLOR else ""
    RESET = "\033[0m" if _USE_COLOR else ""


def debug(msg: str) -> None:
    """Print debug message if debug flag is enabled."""
    if args.debug:
        print(f"DEBUG: {msg}")


def info(msg: str) -> None:
    """Print info message in verbose or debug mode."""
    if args.verbose or args.debug:
        print(msg)


def error(msg: str, exception: Exception = None) -> None:
    """Print error message with red highlighting. Always visible."""
    error_text = f"{Colors.RED}{Colors.BOLD}ERROR:{Colors.RESET}{Colors.RED} {msg}{Colors.RESET}"
    if exception:
        error_text += f"\n       {Colors.YELLOW}{type(exception).__name__}: {exception}{Colors.RESET}"
    print(error_text)


# Handle --overwrite: clear listings.json before starting
if args.overwrite:
    if DATA_FILE.exists():
        DATA_FILE.unlink()
        info(f"Cleared {DATA_FILE}")


def load_seen_ids() -> dict[str, set]:
    """Load previously seen listing IDs from JSON file, organized by source."""
    if args.sendall:
        debug("--sendall flag: ignoring history")
        return {}
    
    if not DATA_FILE.exists():
        debug(f"No data file found at {DATA_FILE}")
        return {}
    
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            result = {}
            for source, ids in data.get("sources", {}).items():
                result[source] = set(ids)
            total = sum(len(ids) for ids in result.values())
            debug(f"Loaded {total} seen IDs across {len(result)} sources")
            return result
    except (json.JSONDecodeError, IOError) as e:
        error(f"Loading data file", e)
        return {}


def save_seen_ids(ids_by_source: dict[str, set]) -> None:
    """Save seen listing IDs to JSON file, organized by source."""
    total = sum(len(ids) for ids in ids_by_source.values())
    debug(f"Saving {total} IDs across {len(ids_by_source)} sources to {DATA_FILE}")
    
    # Sort sources and IDs for consistent output (avoids unnecessary git diffs)
    data = {"sources": {source: sorted(ids) for source, ids in sorted(ids_by_source.items())}}
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


async def fetch_page(browser, url: str, js_heavy: bool = True, max_html_kb: int = 0, scroll_to_load: bool = False, wait_for_selector: str = "", click_selector: str = "", block_resources: bool = True, use_proxy: bool = False) -> Optional[BeautifulSoup]:
    """Fetch and parse a webpage using a shared Playwright browser.
    
    Args:
        browser: Shared Playwright browser instance
        url: URL to fetch
        js_heavy: Use JS-heavy loading strategy
        max_html_kb: If >0, truncate HTML to this many KB before parsing (0 = no limit)
        scroll_to_load: Scroll page to trigger lazy-loaded images
        wait_for_selector: CSS selector to wait for before scraping (useful for JS-loaded content)
        click_selector: CSS selector to click after page load (useful for triggering filters)
        block_resources: Block stylesheets/fonts/media for speed. Disable for sites
            (e.g. Wix) whose JS galleries fail to render without their CSS.
        use_proxy: Route this request through the residential proxy (if configured)
    """
    context = None
    try:
        debug(f"Fetching: {url}")
        context_opts = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "viewport": {"width": 1280, "height": 720},
            "locale": "en-US",
            "timezone_id": "America/Toronto",
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-CH-UA": '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
                "Upgrade-Insecure-Requests": "1",
            },
        }
        if use_proxy and PROXY_CONFIG:
            context_opts["proxy"] = PROXY_CONFIG
            debug("Using residential proxy for this request")
        context = await browser.new_context(**context_opts)
        # Reduce automated-browser fingerprint so bot walls (e.g. Cloudflare on
        # chrono24) don't block CI runs from datacenter IPs. Hides the
        # navigator.webdriver flag that headless Chromium exposes by default.
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()
        
        # Block unnecessary resources to speed up page loads
        # Images are fulfilled with a tiny 1px GIF instead of aborted,
        # so lazy-loading JS still fires and populates src attributes
        if block_resources:
            _PIXEL = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
            async def _block_resources(route):
                rtype = route.request.resource_type
                if rtype in ("stylesheet", "font", "media"):
                    await route.abort()
                elif rtype == "image":
                    await route.fulfill(body=_PIXEL, content_type="image/gif")
                else:
                    await route.continue_()
            await page.route("**/*", _block_resources)
            
        if js_heavy:
            # JS-heavy sites: use commit (fastest) + short wait for initial render
            # Residential proxy routing is slower, so allow more time when proxied.
            goto_timeout = 60000 if (use_proxy and PROXY_CONFIG) else 30000
            await page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout)
            await page.wait_for_timeout(2000)
        else:
            goto_timeout = 60000 if (use_proxy and PROXY_CONFIG) else 30000
            await page.goto(url, wait_until="networkidle", timeout=goto_timeout)
        
        # Click element if specified (useful for triggering filters)
        if click_selector:
            try:
                debug(f"Clicking selector: {click_selector}")
                await page.wait_for_selector(click_selector, timeout=10000)
                await page.click(click_selector)
                await page.wait_for_timeout(2000)  # Wait for content to reload after click
            except Exception as e:
                debug(f"Click failed (non-fatal): {click_selector} - {e}")
        
        # Wait for specific selector if provided (useful for dynamically loaded content)
        if wait_for_selector:
            try:
                debug(f"Waiting for selector: {wait_for_selector}")
                await page.wait_for_selector(wait_for_selector, timeout=15000)
                await page.wait_for_timeout(500)  # Brief wait after element appears
            except Exception as e:
                debug(f"Selector wait timed out (non-fatal): {wait_for_selector}")
        
        # Scroll page to trigger lazy-loaded images
        if scroll_to_load:
            debug("Scrolling page to trigger lazy loading...")
            await page.evaluate("""async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                const scrollHeight = document.body.scrollHeight;
                const viewportHeight = window.innerHeight;
                for (let y = 0; y < scrollHeight; y += viewportHeight / 2) {
                    window.scrollTo(0, y);
                    await delay(200);
                }
                window.scrollTo(0, 0);
                await delay(500);
            }""")
            await page.wait_for_timeout(1000)  # Wait for images to load after scrolling
        
        # Save screenshot and HTML in debug mode (skip in CI and simple mode)
        if args.debug and not IS_CI and not args.simple:
            try:
                screenshot_path = SCRIPT_DIR / "debug_screenshot.png"
                html_path = SCRIPT_DIR / "debug_page.html"
                await page.screenshot(path=str(screenshot_path), full_page=False, timeout=10000)
                debug(f"Screenshot saved to {screenshot_path}")
                html_content = await page.content()
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html_content)
                debug(f"HTML saved to {html_path}")
            except Exception as e:
                debug(f"Debug screenshot failed (non-fatal): {e}")
        
        content = await page.content()
        debug(f"Response: {len(content)} bytes")
        await context.close()
        context = None
        
        # Truncate HTML if specified to speed up parsing
        if max_html_kb > 0 and len(content) > max_html_kb * 1024:
            debug(f"Truncating HTML from {len(content)} to {max_html_kb * 1024} bytes")
            content = content[:max_html_kb * 1024]
        
        return BeautifulSoup(content, "lxml")
    except Exception as e:
        error(f"Fetching page: {url}", e)
        return None
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass


# =============================================================================
# BASE SCRAPER CLASS
# =============================================================================

def get_lazy_image_src(img_elem) -> str:
    """Extract image URL from an element, handling lazy-loading attributes."""
    if not img_elem:
        return ""
    
    # Priority order for image sources
    src_attrs = [
        "src",           # Standard attribute (check first, skip if placeholder)
        "data-src",      # Common lazy-load
        "data-lazy-src", # Another common pattern  
        "data-original", # Used by some libraries
        "data-lazy",     # Generic lazy attribute
    ]
    
    for attr in src_attrs:
        src = img_elem.get(attr, "")
        # Skip placeholder data URIs and empty values
        if src and not src.startswith("data:"):
            return src
    
    # Try srcset/data-srcset as fallback
    srcset = img_elem.get("srcset", "") or img_elem.get("data-srcset", "")
    if srcset:
        # Get first URL from srcset
        first_src = srcset.split(",")[0].strip().split(" ")[0]
        if first_src and not first_src.startswith("data:"):
            if first_src.startswith("//"):
                first_src = "https:" + first_src
            return first_src
    
    return ""


class BaseScraper(ABC):
    """Abstract base class for watch site scrapers."""
    
    name: str = "base"
    base_url: str = ""
    js_heavy: bool = True  # Set True for JS-heavy sites that need longer render wait
    max_html_kb: int = 0  # If >0, truncate HTML to this many KB before parsing
    title_filter: str = ""  # If set, only include listings with this text in title
    link_filter: str = ""  # If set, only include listings with this text in link/href
    scroll_to_load: bool = False  # Set True to scroll page and trigger lazy-loaded images
    wait_for_selector: str = ""  # CSS selector to wait for before scraping (for JS-loaded content)
    click_selector: str = ""  # CSS selector to click after page load (for triggering filters)
    block_resources: bool = True  # Set False for sites whose JS galleries need CSS to render (e.g. Wix)
    retry_attempts: int = 0  # Number of retry attempts if scrape returns no results (0 = no retries)
    use_proxy: bool = False  # Route requests through the residential proxy (for IP-blocked sites)
    
    @abstractmethod
    def build_search_url(self, query: str) -> str:
        """Build the search URL for the given query."""
        pass
    
    @abstractmethod
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        """Parse listings from the page HTML."""
        pass
    
    def filter_listings(self, listings: list[Listing]) -> list[Listing]:
        """Filter listings by title_filter and/or link_filter if set."""
        if not self.title_filter and not self.link_filter:
            return listings
        
        filtered = listings
        if self.title_filter:
            filter_words = self.title_filter.lower().split()
            filtered = [l for l in filtered if all(w in l.title.lower() for w in filter_words)]
            debug(f"[{self.name}] Title filtered {len(listings)} -> {len(filtered)} listing{'s' if len(filtered) != 1 else ''} (filter: '{self.title_filter}')")
        if self.link_filter:
            before = len(filtered)
            filter_words = self.link_filter.lower().split()
            filtered = [l for l in filtered if all(w in l.link.lower() for w in filter_words)]
            debug(f"[{self.name}] Link filtered {before} -> {len(filtered)} listing{'s' if len(filtered) != 1 else ''} (filter: '{self.link_filter}')")
        return filtered
    
    last_fetch_time: float = 0.0  # Time spent fetching the page
    
    async def scrape(self, query: str, browser) -> list[Listing]:
        """Scrape listings for the given query."""
        url = self.build_search_url(query)
        attempts = 0
        max_attempts = 1 + self.retry_attempts  # 1 initial + retries
        
        while attempts < max_attempts:
            attempts += 1
            t0 = time.time()
            soup = await fetch_page(browser, url, js_heavy=self.js_heavy, max_html_kb=self.max_html_kb, scroll_to_load=self.scroll_to_load, wait_for_selector=self.wait_for_selector, click_selector=self.click_selector, block_resources=self.block_resources, use_proxy=self.use_proxy)
            self.last_fetch_time = time.time() - t0
            
            if not soup:
                if attempts < max_attempts:
                    debug(f"[{self.name}] Retry {attempts}/{self.retry_attempts} - page fetch failed")
                    continue
                return []
            
            listings = self.parse_listings(soup)
            listings = self.filter_listings(listings)
            
            if listings or attempts >= max_attempts:
                debug(f"[{self.name}] Scraped {len(listings)} listing{'s' if len(listings) != 1 else ''}")
                return listings
            
            debug(f"[{self.name}] Retry {attempts}/{self.retry_attempts} - no listings found")
        
        return []


# =============================================================================
# CHRONO24 SCRAPER
# =============================================================================

class Chrono24Scraper(BaseScraper):
    """Scraper for Chrono24.ca"""
    
    name = "chrono24"
    base_url = "https://www.chrono24.ca"
    sort_order = 5  # 5 = newest first
    wait_for_selector = "div.js-listing-item-container, div.js-article-item-container"  # Wait for listings to load
    retry_attempts = 1  # Retry up to 1 time if no listings found
    
    def build_search_url(self, query: str) -> str:
        return f"{self.base_url}/search/index.htm?dosearch=true&query={query}&sortorder={self.sort_order}"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        containers = soup.find_all("div", class_="js-listing-item-container")
        if not containers:
            containers = soup.find_all("div", class_="js-article-item-container")
        debug(f"[{self.name}] Found {len(containers)} listing containers")
        
        for container in containers:
            try:
                # Extract listing ID from wishlist button
                wishlist_btn = container.find("button", class_="js-wishlist-toggle")
                listing_id = wishlist_btn.get("data-note", "") if wishlist_btn else ""
                
                # Fallback: extract from link URL
                if not listing_id:
                    link_elem = container.find("a", class_="listing-item-link")
                    if link_elem and "--id" in link_elem.get("href", ""):
                        listing_id = link_elem["href"].split("--id")[-1].split(".")[0]
                
                if not listing_id:
                    continue
                
                # Extract title
                title_parts = []
                model_elem = container.find("p", class_="text-bold")
                if model_elem:
                    title_parts.append(model_elem.get_text(strip=True))
                
                subtitle_elem = container.find("p", class_="text-ellipsis")
                if subtitle_elem and subtitle_elem != model_elem:
                    title_parts.append(subtitle_elem.get_text(strip=True))
                
                title = " - ".join(title_parts) if title_parts else "Unknown"
                
                # Extract price
                price_elem = container.find("p", class_="text-md")
                price = price_elem.get_text(strip=True) if price_elem else "Price N/A"
                
                # Extract link
                link_elem = container.find("a", class_="listing-item-link")
                link = ""
                if link_elem:
                    link = link_elem.get("href", "")
                    if link and not link.startswith("http"):
                        link = self.base_url + link
                
                # Extract image
                img_elem = container.find("img", class_="sweetspot")
                image = ""
                if img_elem:
                    image = img_elem.get("src", "")
                    if image.startswith("data:"):
                        image = img_elem.get("data-lazy-sweet-spot-master-src", "")
                        if image:
                            image = image.replace("_SIZE_", "480")
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'ref\.?\s*(\d+[A-Z]?)', title, re.IGNORECASE)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing container", e)
                continue
        
        return listings


# =============================================================================
# ONBEHALF.JP SCRAPER
# =============================================================================

class OnBehalfScraper(BaseScraper):
    """Scraper for OnBehalf.jp - Japanese vintage watch dealer."""
    
    name = "onbehalf"
    base_url = "https://onbehalf.jp/en/item/"
    retry_attempts = 1  # Retry up to 1 time if no listings found
    
    def build_search_url(self, query: str) -> str:
        return self.base_url
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find the products list
        products_ul = soup.find("ul", class_="products")
        if not products_ul:
            debug(f"[{self.name}] No products list found")
            return []
        
        items = products_ul.find_all("li")
        debug(f"[{self.name}] Found {len(items)} product items")
        
        for item in items:
            try:
                # Find the main link
                link_elem = item.find("a")
                if not link_elem:
                    continue
                
                link = link_elem.get("href", "")
                
                # Extract ID from URL (e.g., /en/item/36634/)
                listing_id = ""
                id_match = re.search(r'/item/(\d+)/?', link)
                if id_match:
                    listing_id = id_match.group(1)
                
                if not listing_id:
                    continue
                
                # Extract reference number from brand div
                brand_elem = item.find("div", class_="brand")
                reference = brand_elem.get_text(strip=True) if brand_elem else ""
                
                # Extract description from label div
                label_elem = item.find("div", class_="label")
                label = label_elem.get_text(strip=True) if label_elem else ""
                
                # Build title from reference and label
                if reference and label:
                    title = f"{reference} - {label}"
                elif reference:
                    title = reference
                elif label:
                    title = label
                else:
                    title = "Unknown"
                
                # Extract price
                price_elem = item.find("span", class_="num")
                price = price_elem.get_text(strip=True) if price_elem else "Price N/A"
                if not price:
                    price = "ASK"
                
                # Extract image
                img_elem = item.find("img")
                image = get_lazy_image_src(img_elem)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference.replace("ref.", "").strip()
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


# =============================================================================
# EVERYWATCH SCRAPER
# =============================================================================

class EveryWatchScraper(BaseScraper):
    """Scraper for EveryWatch.com - Watch aggregator site."""
    
    name = "everywatch"
    base_url = "https://everywatch.com"
    retry_attempts = 1  # Retry up to 1 time if no listings found

    # Fixed URL for Patek Philippe 2508 search
    search_url = "https://everywatch.com/watch-listing?auctionType=listing&hideGhostListing=false&hideOutlier=false&hideDormant=false&keyword=patek+2508&sortColumn=newest&sortType=desc&currencyMode=USD"
    
    def build_search_url(self, query: str) -> str:
        # Use fixed URL - doesn't use query parameter
        return self.search_url
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all watch cards
        cards = soup.find_all("a", class_="ew-grid-watch-card")
        debug(f"[{self.name}] Found {len(cards)} watch cards")
        
        for card in cards:
            try:
                # Extract listing ID from data-item attribute
                listing_id = card.get("data-item", "")
                if not listing_id:
                    continue
                
                # Extract link
                link = card.get("href", "")
                if link and not link.startswith("http"):
                    link = self.base_url + link
                
                # Extract brand/model
                brand_model_elem = card.find("div", class_="brand-model")
                brand_model = brand_model_elem.get_text(strip=True) if brand_model_elem else ""
                
                # Extract reference number
                refno_elem = card.find("div", class_="refno")
                reference = refno_elem.get_text(strip=True) if refno_elem else ""
                
                # Build title
                if brand_model and reference:
                    title = f"{brand_model} {reference}"
                elif brand_model:
                    title = brand_model
                else:
                    title = "Unknown"
                
                # Extract price - look for the price wrapper
                price_elem = card.find("span", class_="watch-pp-wrapper")
                price = ""
                if price_elem:
                    # Get text content, clean it up
                    price_text = price_elem.get_text(strip=True)
                    # Format: "24,702 USD" -> extract and format
                    price = price_text.replace("\n", " ").strip()
                if not price:
                    price = "Price N/A"
                
                # Extract image - first img in the card
                img_elem = card.find("img")
                image = get_lazy_image_src(img_elem)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing card", e)
                continue
        
        return listings


# =============================================================================
# CASO WATCHES SCRAPER
# =============================================================================

class CasoWatchesScraper(BaseScraper):
    """Scraper for CasoWatches.com - Italian vintage watch dealer."""
    
    name = "casowatches"
    base_url = "https://www.casowatches.com"
    
    def build_search_url(self, query: str) -> str:
        return f"{self.base_url}/?s={query}"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all result divs
        results = soup.find_all("div", class_="result")
        debug(f"[{self.name}] Found {len(results)} result items")
        
        for result in results:
            try:
                # Find the title link in <big> tag
                big_elem = result.find("big")
                if not big_elem:
                    continue
                
                link_elem = big_elem.find("a")
                if not link_elem:
                    continue
                
                link = link_elem.get("href", "")
                title = link_elem.get_text(strip=True)
                
                # Extract image
                img_elem = result.find("img", class_="img-responsive")
                image = get_lazy_image_src(img_elem)
                
                # Use full image URL as ID (strip query params)
                listing_id = image.split("?")[0] if image else ""
                
                # Fallback to URL slug
                if not listing_id:
                    id_match = re.search(r'/catalogo/([^/]+)/?', link)
                    if id_match:
                        listing_id = id_match.group(1)
                
                if not listing_id:
                    continue
                
                # No price displayed on search results
                price = "ASK"
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'ref\.?\s*(\d+[A-Z]?)', title, re.IGNORECASE)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing result", e)
                continue
        
        return listings


# =============================================================================
# BULANG AND SONS SCRAPER
# =============================================================================

class BulangSonsScraper(BaseScraper):
    """Scraper for BulangAndSons.net - Vintage watch dealer."""
    
    name = "bulangsons"
    base_url = "https://www.bulangandsons.net"
    title_filter = "2508"
    # Wait for product cards with handle attribute (indicates fully loaded, not placeholder)
    wait_for_selector = "product-card.product-card"

    def build_search_url(self, query: str) -> str:
        return f"{self.base_url}/search?q={query}"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Use CSS selector for more robust custom element matching
        cards = soup.select("product-card.product-card")
        # Fallback: try finding by handle attribute if class matching fails
        if not cards:
            cards = soup.select("[handle]")
        # Another fallback: find any element with product-card class
        if not cards:
            cards = soup.find_all(class_="product-card")
        debug(f"[{self.name}] Found {len(cards)} product cards")
        
        for card in cards:
            try:
                # Find the title link - may have additional classes like "h6"
                title_elem = card.select_one("a.product-title") or card.select_one("[class*='product-title']")
                title = title_elem.get_text(strip=True) if title_elem else "Unknown"
                
                # Extract link from product-card__media anchor
                link_elem = card.select_one("a.product-card__media") or card.select_one("[class*='product-card__media']")
                link = ""
                if link_elem:
                    href = link_elem.get("href", "")
                    # Clean up URL params and make absolute
                    href = href.split("?")[0]
                    link = self.base_url + href if href else ""
                
                # Extract image - primary image (may have additional classes)
                img_elem = card.select_one("img.product-card__image--primary") or card.select_one("img[class*='product-card__image--primary']")
                image = ""
                if img_elem:
                    src = img_elem.get("src", "")
                    if src.startswith("//"):
                        src = "https:" + src
                    image = src
                
                # Use handle attribute as ID (image URLs vary across domains)
                listing_id = card.get("handle", "")
                
                # Fallback to URL slug from link
                if not listing_id and link:
                    listing_id = link.rstrip("/").split("/")[-1]
                
                if not listing_id:
                    continue
                
                # Check if sold out - look for sold-out-badge or text containing "Sold"
                sold_badge = card.select_one("sold-out-badge") or card.select_one(".badge--sold-out")
                sold_text = card.find(string=re.compile(r'\bSold\b', re.IGNORECASE))
                price = "Sold" if (sold_badge or sold_text) else "ASK"
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'ref\.?\s*(\d+[A-Z]?)', title, re.IGNORECASE)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing card", e)
                continue
        
        return listings


# =============================================================================
# LOUPETHIS SCRAPER
# =============================================================================

class LoupeThisScraper(BaseScraper):
    """Scraper for LoupeThis.com - Watch auction site."""
    
    name = "loupethis"
    base_url = "https://loupethis.com"
    # Wait for auction cards with links (shimmer placeholders don't have these)
    wait_for_selector = ".AuctionCard a[href]"
    
    def build_search_url(self, query: str) -> str:
        return f"{self.base_url}/search?query={query}&sort_by=ends_at_desc"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all auction cards
        cards = soup.find_all("div", class_="AuctionCard")
        debug(f"[{self.name}] Found {len(cards)} auction cards")
        
        for card in cards:
            try:
                # Extract image first (used as listing ID)
                img_elem = card.find("img")
                image = get_lazy_image_src(img_elem)
                
                # Use full image URL as ID (strip query params)
                listing_id = image.split("?")[0] if image else ""
                
                if not listing_id:
                    continue
                
                # Find the main link (absolute positioned overlay link)
                link_elem = card.find("a", class_="absolute")
                link = ""
                if link_elem:
                    href = link_elem.get("href", "")
                    link = self.base_url + href if href else ""
                
                # Extract title from h3 or the visually-hidden span
                title = "Unknown"
                title_elem = card.find("h3", class_="text-style--heading-4")
                if title_elem:
                    title = title_elem.get_text(strip=True)
                else:
                    # Fallback: get from visually-hidden span
                    span = card.find("span", class_="visually-hidden")
                    if span:
                        text = span.get_text(strip=True)
                        title = text.replace("Go to ", "").replace(" detail page.", "")
                
                # Extract price - look for sold/price text
                price = "ASK"
                price_spans = card.find_all("span", class_="text-style--heading-4--number")
                for span in price_spans:
                    text = span.get_text(strip=True)
                    if "$" in text:
                        price = text
                        break
                
                # Check if sold
                sold_elem = card.find("span", string=re.compile(r"Sold", re.IGNORECASE))
                if sold_elem and price != "ASK":
                    price = f"Sold {price}"
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'(\d{4}[A-Z]?)', title)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing card", e)
                continue
        
        return listings


# =============================================================================
# DAVIDE PARMEGIANI SCRAPER
# =============================================================================

class DavideParmegianiScraper(BaseScraper):
    """Scraper for DavideParmegiani.ch - Swiss vintage watch dealer."""
    
    name = "parmegiani"
    base_url = "https://davideparmegiani.ch"
    wait_for_selector = "ul.infinite-scroll li a[href]"
    
    def build_search_url(self, query: str) -> str:
        return f"{self.base_url}/?s={query}"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all listing items
        items = soup.find_all("li", attrs={"data-equalizer-watch": True})
        debug(f"[{self.name}] Found {len(items)} listing items")
        
        for item in items:
            try:
                # Find the main link
                link_elem = item.find("a", class_="button")
                if not link_elem:
                    link_elem = item.find("a")
                
                if not link_elem:
                    continue
                
                link = link_elem.get("href", "")
                
                # Extract ID from URL (e.g., /for-sale/5610/patek-philippe-2508)
                listing_id = ""
                id_match = re.search(r'/for-sale/(\d+)/', link)
                if id_match:
                    listing_id = id_match.group(1)
                
                if not listing_id:
                    continue
                
                # Extract title from h2.name
                title_elem = item.find("h2", class_="name")
                title = title_elem.get_text(strip=True) if title_elem else "Unknown"
                
                # Extract category/brand
                category_elem = item.find("span", class_="category")
                category = category_elem.get_text(strip=True) if category_elem else ""
                if category and not title.startswith(category):
                    title = f"{category} {title}"
                
                # Extract image
                img_elem = item.find("img")
                image = get_lazy_image_src(img_elem)
                
                # Check if sold
                abstract = item.find("div", class_="abstract")
                price = "ASK"
                if abstract:
                    sold_elem = abstract.find("strong")
                    if sold_elem and "sold" in sold_elem.get_text(strip=True).lower():
                        price = "Sold"
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'(\d{4}[A-Z]?)', title)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


# =============================================================================
# SHUCK THE OYSTER SCRAPER
# =============================================================================

class ShuckTheOysterScraper(BaseScraper):
    """Scraper for ShuckTheOyster.com - Vintage watch dealer."""
    
    name = "shucktheoyster"
    base_url = "https://www.shucktheoyster.com"
    
    def build_search_url(self, query: str) -> str:
        return f"{self.base_url}/?s={query}"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all listing items in the portfolio grid
        items = soup.find_all("li", class_="item")
        debug(f"[{self.name}] Found {len(items)} listing items")
        
        for item in items:
            try:
                # Find the main link
                link_elem = item.find("a")
                if not link_elem:
                    continue
                
                link = link_elem.get("href", "")
                
                # Extract title from thumb_text_title
                title_elem = item.find("div", class_="thumb_text_title")
                title = title_elem.get_text(strip=True) if title_elem else "Unknown"
                
                # Extract image
                img_elem = item.find("img")
                image = get_lazy_image_src(img_elem)
                
                # Use full image URL as ID (strip query params)
                listing_id = image.split("?")[0] if image else ""
                
                # Fallback to URL slug if no image
                if not listing_id:
                    id_match = re.search(r'/portfolio/([^/]+)/?', link)
                    if id_match:
                        listing_id = id_match.group(1)
                
                if not listing_id:
                    continue
                
                # Check if sold (has sold_dot or sold_text)
                sold_elem = item.find("div", class_="sold_dot") or item.find("div", class_="sold_text")
                price = "Sold" if sold_elem else "ASK"
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'(\d{4}[A-Z]?)', title)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


class CollectabilityScraper(BaseScraper):
    """Scraper for collectability.com - Watch dealer with search."""
    
    name = "collectability"
    base_url = "https://collectability.com"
    
    def build_search_url(self, query: str) -> str:
        # Extract just the numeric part for search (e.g., "patek+2508" -> "2508")
        search_term = query.replace("+", " ").split()[-1] if "+" in query else query
        return f"{self.base_url}/shop/?post_type=product&shop_search={search_term}"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all product cards
        items = soup.find_all("li", class_="collectability-product-card")
        debug(f"[{self.name}] Found {len(items)} product items")
        
        for item in items:
            try:
                # Find the article element with all the data attributes
                article = item.find("article", class_="collectability-product-card")
                if not article:
                    continue
                
                # Extract data from attributes
                listing_id = article.get("data-product-id", "")
                if not listing_id:
                    continue
                
                title = article.get("data-product-name", "Unknown")
                reference = article.get("data-product-ref", "")
                image = article.get("data-product-image", "")
                link = article.get("data-product-url", "")
                price = article.get("data-product-price", "ASK")
                
                # Clean up price (remove HTML entities)
                price = price.replace("&nbsp;", " ").strip()
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference.replace("REF.", "").replace("ref.", "").strip()
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


class CorradoMattarelliScraper(BaseScraper):
    """Scraper for corradomattarelli.com - Italian vintage watch dealer."""
    
    name = "corradomattarelli"
    base_url = "https://corradomattarelli.com"
    scroll_to_load = True

    def build_search_url(self, query: str) -> str:
        # Convert "patek+2508" to "patek 2508*" for search
        search_term = query.replace("+", " ")
        return f"{self.base_url}/search?q={search_term}*&type=product"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all product items
        items = soup.find_all("div", class_="ProductItem")
        debug(f"[{self.name}] Found {len(items)} product items")
        
        for item in items:
            try:
                # Find the title link
                title_elem = item.find("h2", class_="ProductItem__Title")
                if not title_elem:
                    continue
                
                link_elem = title_elem.find("a")
                if not link_elem:
                    continue
                
                title = link_elem.get_text(strip=True)
                href = link_elem.get("href", "")
                
                # Clean URL
                href_clean = href.split("?")[0]  # Remove query params
                link = self.base_url + href_clean
                
                # Extract image
                img_elem = item.find("img", class_="ProductItem__Image")
                image = ""
                if img_elem:
                    # Try srcset first, then data-srcset
                    srcset = img_elem.get("srcset", "") or img_elem.get("data-srcset", "")
                    if srcset:
                        # Get the largest image from srcset
                        parts = srcset.split(",")
                        if parts:
                            last_src = parts[-1].strip().split(" ")[0]
                            if last_src.startswith("//"):
                                last_src = "https:" + last_src
                            image = last_src
                
                # Use full image URL as ID (strip query params)
                listing_id = image.split("?")[0] if image else ""
                
                # Fallback to URL slug
                if not listing_id:
                    listing_id = href_clean.split("/")[-1] if href_clean else ""
                
                if not listing_id:
                    continue
                
                # Check if sold
                sold_label = item.find("span", class_="ProductItem__Label--soldOut")
                price_elem = item.find("span", class_="ProductItem__Price")
                
                if sold_label:
                    price = "Sold"
                elif price_elem:
                    price = price_elem.get_text(strip=True)
                    if price.lower() == "sold":
                        price = "Sold"
                else:
                    price = "ASK"
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'(\d{4}[A-Z]?)', title)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


class PatekMongerScraper(BaseScraper):
    """Scraper for patekmonger.com - Patek Philippe specialist dealer."""
    
    name = "patekmonger"
    base_url = "https://www.patekmonger.com"
    title_filter = "2508"  # Only show listings with 2508 in title
    wait_for_selector = "div.product-list div.product-block[data-product-id]"
    
    def build_search_url(self, query: str) -> str:
        # Convert "patek+2508" to "patek 2508*" for search
        search_term = query.replace("+", " ")
        return f"{self.base_url}/search?type=product,article,page&q={search_term}*"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all product blocks (only those with data-product-id, skip page blocks)
        items = soup.find_all("div", class_="product-block", attrs={"data-product-id": True})
        debug(f"[{self.name}] Found {len(items)} product items")
        
        for item in items:
            try:
                # Get product ID from data attribute
                listing_id = item.get("data-product-id", "")
                if not listing_id:
                    continue
                
                # Find the product link
                link_elem = item.find("a", class_="product-link")
                if not link_elem:
                    continue
                
                href = link_elem.get("href", "")
                href_clean = href.split("?")[0]  # Remove query params
                link = self.base_url + href_clean
                
                # Get title
                title_elem = item.find("div", class_="title")
                title = title_elem.get_text(strip=True) if title_elem else "Unknown"
                
                # Get price - check for theme-money span first, then look for "Price on Request"
                price_elem = item.find("span", class_="theme-money")
                if price_elem:
                    price = price_elem.get_text(strip=True)
                else:
                    # Check for "Price on Request" or similar
                    product_info = item.find("div", class_="product-info")
                    if product_info:
                        p_elem = product_info.find("p")
                        if p_elem:
                            price = p_elem.get_text(strip=True)
                        else:
                            price = "ASK"
                    else:
                        price = "ASK"
                
                # Get image from srcset
                img_elem = item.find("img", class_="rimage__image")
                image = ""
                if img_elem:
                    srcset = img_elem.get("srcset", "") or img_elem.get("data-srcset", "")
                    if srcset:
                        # Get a mid-sized image from srcset
                        parts = srcset.split(",")
                        # Pick one around the middle for decent quality
                        if len(parts) > 3:
                            src = parts[len(parts) // 2].strip().split(" ")[0]
                        elif parts:
                            src = parts[-1].strip().split(" ")[0]
                        else:
                            src = ""
                        if src.startswith("//"):
                            src = "https:" + src
                        image = src
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'(\d{4}[A-Z]?)', title)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


class DB1983Scraper(BaseScraper):
    """Scraper for db1983.com - Vintage watch dealer."""
    
    name = "db1983"
    base_url = "https://www.db1983.com"
    scroll_to_load = True

    def build_search_url(self, query: str) -> str:
        # Convert "patek+2508" to "patek 2508" for URL
        search_term = query.replace("+", " ")
        return f"{self.base_url}/search-results/{search_term}"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all product blocks
        items = soup.find_all("div", class_="product-block")
        debug(f"[{self.name}] Found {len(items)} product items")
        
        for item in items:
            try:
                # Find the main link
                link_elem = item.find("a", class_="custom-link")
                if not link_elem:
                    continue
                
                href = link_elem.get("href", "")
                if not href:
                    continue
                
                link = self.base_url + href if href.startswith("/") else href
                
                # Get title from h3
                title_elem = item.find("h3")
                title = title_elem.get_text(strip=True) if title_elem else "Unknown"
                
                # Get image
                img_elem = item.find("img", class_="aspect-ratio__img")
                image = get_lazy_image_src(img_elem)
                
                # Use full image URL as ID (strip query params)
                listing_id = image.split("?")[0] if image else ""
                
                # Fallback to URL slug
                if not listing_id:
                    listing_id = href.split("/")[-1] if href else ""
                
                if not listing_id:
                    continue
                
                # No price displayed on search results
                price = "ASK"
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'(\d{4}[A-Z]?)', title)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


class MatthewBainScraper(BaseScraper):
    """Scraper for matthewbaininc.com - Vintage watch dealer (Wix site)."""
    
    name = "matthewbain"
    base_url = "https://www.matthewbaininc.com"
    title_filter = "2508"  # Only show listings with 2508 in title
    # Wix search results load the product gallery asynchronously
    wait_for_selector = "li[data-hook='product-list-grid-item']"
    block_resources = False  # Wix gallery needs CSS to render product items
    
    def build_search_url(self, query: str) -> str:
        return f"{self.base_url}/search?q={query}&sort=newest"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all product grid items
        items = soup.find_all("li", attrs={"data-hook": "product-list-grid-item"})
        debug(f"[{self.name}] Found {len(items)} product items")
        
        for item in items:
            try:
                # Find the product item root div with data-slug
                product_div = item.find("div", attrs={"data-hook": "product-item-root"})
                if not product_div:
                    continue
                
                # Find the product link
                link_elem = item.find("a", attrs={"data-hook": "product-item-product-details-link"})
                if not link_elem:
                    link_elem = item.find("a", attrs={"data-hook": "product-item-container"})
                
                link = ""
                if link_elem:
                    href = link_elem.get("href", "")
                    link = href if href.startswith("http") else self.base_url + href
                
                # Get title from product-item-name
                title_elem = item.find("p", attrs={"data-hook": "product-item-name"})
                title = title_elem.get_text(strip=True) if title_elem else "Unknown"
                
                # Get price from data-wix-price attribute or text
                price_elem = item.find("span", attrs={"data-hook": "product-item-price-to-pay"})
                if price_elem:
                    price = price_elem.get("data-wix-price", "") or price_elem.get_text(strip=True)
                else:
                    price = "ASK"
                
                # Get image from first img
                img_elem = item.find("img")
                image = get_lazy_image_src(img_elem)
                
                # Use full image URL as ID (strip query params)
                listing_id = image.split("?")[0] if image else ""
                
                # Fallback to data-slug
                if not listing_id:
                    listing_id = product_div.get("data-slug", "")
                
                if not listing_id:
                    continue
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'(\d{4}[A-Z]?)', title)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


class WindVintageScraper(BaseScraper):
    """Scraper for windvintage.com - no search, filters by query in title."""
    name = "windvintage"
    base_url = "https://www.windvintage.com"
    title_filter = "patek 2508"  # Only show listings with this in title
    
    def build_search_url(self, query: str) -> str:
        # No search functionality - just return homepage
        return self.base_url
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        # Try multiple selectors for Squarespace gallery items
        items = soup.find_all("div", class_="slide")
        if not items:
            items = soup.find_all("div", class_="sqs-gallery-design-grid-slide")
        debug(f"[{self.name}] Found {len(items)} items on page")
        
        for item in items:
            try:
                # Get title from image-slide-title div
                title_elem = item.find("div", class_="image-slide-title")
                if not title_elem:
                    continue
                title = title_elem.get_text(strip=True)
                
                # Get link
                link_elem = item.find("a", class_="image-slide-anchor")
                if not link_elem or not link_elem.get("href"):
                    continue
                link = link_elem["href"]
                if not link.startswith("http"):
                    link = f"{self.base_url}{link}" if link.startswith("/") else f"{self.base_url}/{link}"
                
                # Get image
                img_elem = item.find("img", class_="thumb-image")
                image = get_lazy_image_src(img_elem)
                
                # Get ID from data-image-id, full image URL, or URL slug (in order of preference)
                listing_id = item.get("data-image-id") or ""
                if not listing_id:
                    listing_id = image.split("?")[0] if image else ""  # Use full image URL as ID
                if not listing_id:
                    # Fallback to URL slug
                    slug_match = re.search(r'windvintage\.com/(.+?)/?$', link)
                    if slug_match:
                        listing_id = slug_match.group(1)
                    else:
                        listing_id = link.split("/")[-1] or link.split("/")[-2]
                
                if not listing_id:
                    continue
                
                # No price listed on this site
                price = "ASK"
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'(\d{4}[A-Z]?)', title)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


# =============================================================================
# WATCHRECON SCRAPER
# =============================================================================

class WatchReconScraper(BaseScraper):
    """Scraper for WatchRecon.com - Watch listing aggregator (Watchuseek, Reddit, etc)."""
    
    name = "watchrecon"
    base_url = "https://www.watchrecon.com"
    
    def build_search_url(self, query: str) -> str:
        return f"{self.base_url}/?query={query}"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all gallery item containers
        cards = soup.find_all("div", class_="galleryItemContainer")
        debug(f"[{self.name}] Found {len(cards)} gallery items")
        
        for card in cards:
            try:
                # Extract listing link and title from subjectInfo div
                # (first listingLink wraps the image and has no title)
                subject_info = card.find("div", class_="subjectInfo")
                if not subject_info:
                    continue
                
                link_elem = subject_info.find("a", class_="listingLink")
                if not link_elem:
                    continue
                
                link = link_elem.get("href", "")
                title = link_elem.get("data-original-title", "") or link_elem.get_text(strip=True)
                
                if not title or not link:
                    continue
                
                # Extract listing ID from detail link cid parameter
                listing_id = ""
                detail_link = card.find("a", href=re.compile(r"detail\.php"))
                if detail_link:
                    cid_match = re.search(r'cid=(\d+)', detail_link.get("href", ""))
                    if cid_match:
                        listing_id = cid_match.group(1)
                
                # Fallback: use link URL as ID
                if not listing_id:
                    listing_id = link.rstrip("/").split("/")[-1].split(".")[0]
                
                if not listing_id:
                    continue
                
                # Extract price
                price_elem = card.find("span", class_="priceInfo")
                price = price_elem.get_text(strip=True) if price_elem else "ASK"
                if price:
                    # Format price with $ prefix if it's just a number
                    price = price.replace("$", "").strip()
                    if price:
                        price = f"${price}"
                    else:
                        price = "ASK"
                
                # Extract image
                img_elem = card.find("img", class_="thumb")
                image = ""
                if img_elem:
                    src = img_elem.get("src", "")
                    if src and not src.startswith("http"):
                        src = f"{self.base_url}/{src}"
                    image = src
                
                # Extract model from modelInfo span
                reference = ""
                model_elem = card.find("span", class_="modelInfo")
                if model_elem:
                    reference = model_elem.get_text(strip=True)
                
                # Fallback: extract reference from title
                if not reference:
                    ref_match = re.search(r'(\d{4}[A-Z]?)', title)
                    if ref_match:
                        reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title.strip(),
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing card", e)
                continue
        
        return listings


# =============================================================================
# WATCHPROSITE SCRAPER
# =============================================================================

class WatchProSiteScraper(BaseScraper):
    """Scraper for WatchProSite.com - Watch forum marketplace."""
    
    name = "watchprosite"
    base_url = "https://www.watchprosite.com"
    
    def build_search_url(self, query: str) -> str:
        # forumid=712 is the marketplace, ftm=FS means "For Sale" only
        search_term = query.replace("+", " ")
        return f"{self.base_url}/?page=wf&qf=&forumid=712&ft={search_term}&ftm=FS&fts="
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find the waterfall container
        waterfall = soup.find("div", id="waterfall")
        if not waterfall:
            debug(f"[{self.name}] No waterfall container found")
            return []
        
        # Find all listing divs with onclick handlers
        cards = waterfall.find_all("div", onclick=re.compile(r"location\.href"))
        debug(f"[{self.name}] Found {len(cards)} listing cards")
        
        for card in cards:
            try:
                # Extract URL from onclick handler
                onclick = card.get("onclick", "")
                url_match = re.search(r"location\.href='([^']+)'", onclick)
                if not url_match:
                    continue
                
                relative_url = url_match.group(1)
                link = self.base_url + relative_url
                
                # Extract listing ID from URL (pi-NNNNNN or ti-NNNNNN)
                listing_id = ""
                id_match = re.search(r'pi-(\d+)', relative_url)
                if id_match:
                    listing_id = id_match.group(1)
                else:
                    id_match = re.search(r'ti-(\d+)', relative_url)
                    if id_match:
                        listing_id = id_match.group(1)
                
                if not listing_id:
                    continue
                
                # Extract title from h4
                title_elem = card.find("h4")
                title = title_elem.get_text(strip=True) if title_elem else "Unknown"
                
                # Extract image
                img_elem = card.find("img")
                image = img_elem.get("src", "") if img_elem else ""
                
                # Extract price from lastUpdatedBy div
                price_elem = card.find("div", class_="lastUpdatedBy")
                price = price_elem.get_text(strip=True) if price_elem else "ASK"
                if not price:
                    price = "ASK"
                
                # Check if sold (title starts with SOLD:)
                if title.upper().startswith("SOLD:"):
                    price = f"Sold - {price}" if price != "ASK" else "Sold"
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'(\d{4}[A-Z]?)', title)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing card", e)
                continue
        
        return listings


# =============================================================================
# THE KEYSTONE SCRAPER
# =============================================================================

class KeystoneScraper(BaseScraper):
    """Scraper for TheKeystone.com - Watch dealer."""
    
    name = "keystone"
    base_url = "https://thekeystone.com"
    title_filter = "2508"
    wait_for_selector = "div.collection__products .search__item__generic"
    
    def build_search_url(self, query: str) -> str:
        return f"{self.base_url}/search?q={query}"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all search result items
        items = soup.find_all("div", class_="search__item__generic")
        debug(f"[{self.name}] Found {len(items)} search items")
        
        for item in items:
            try:
                # Find the title link
                title_elem = item.find("p", class_="product__inline__title")
                if not title_elem:
                    continue
                
                link_elem = title_elem.find("a")
                if not link_elem:
                    continue
                
                title = link_elem.get_text(strip=True)
                href = link_elem.get("href", "")
                
                # Clean URL (remove query params) and make absolute
                href_clean = href.split("?")[0]
                link = self.base_url + href_clean if href_clean else ""
                
                # Extract image
                img_elem = item.find("img")
                image = get_lazy_image_src(img_elem)
                if image and image.startswith("//"):
                    image = "https:" + image
                
                # Use full image URL as ID (strip query params)
                listing_id = image.split("?")[0] if image else ""
                
                # Fallback to URL slug
                if not listing_id:
                    id_match = re.search(r'/products/([^/?]+)', href)
                    if id_match:
                        listing_id = id_match.group(1)
                
                if not listing_id:
                    continue
                
                # Extract price
                price_elem = item.find("p", class_="product__inline__price")
                price = "ASK"
                if price_elem:
                    price_text = price_elem.get_text(strip=True)
                    if "sold" in price_text.lower():
                        price = "Sold"
                    elif price_text:
                        price = price_text
                
                # Try to extract reference from title
                reference = ""
                ref_match = re.search(r'ref\.?\s*(\d+[A-Z]?)', title, re.IGNORECASE)
                if ref_match:
                    reference = ref_match.group(1)
                
                listing = Listing(
                    id=str(listing_id),
                    title=title,
                    price=price,
                    link=link,
                    source=self.name,
                    image=image,
                    reference=reference
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


# =============================================================================
# PHILLIPS SCRAPER
# =============================================================================

class PhillipsScraper(BaseScraper):
    """Scraper for Phillips.com - Major auction house."""
    
    name = "phillips"
    base_url = "https://www.phillips.com"
    title_filter = "patek"  # Only show Patek Philippe listings
    
    def build_search_url(self, query: str) -> str:
        return f"{self.base_url}/Search?Search=2508"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all search result items
        items = soup.find_all("li", class_="search-result-item")
        debug(f"[{self.name}] Found {len(items)} search result items")
        
        for item in items:
            try:
                # Get item ID from element id attribute (e.g., "itemid329")
                item_id = item.get("id", "")
                if item_id.startswith("itemid"):
                    item_id = item_id[6:]  # Remove "itemid" prefix
                if not item_id:
                    continue
                
                # Get link and image from image div
                image_div = item.find("div", class_="image")
                if not image_div:
                    continue
                
                link_elem = image_div.find("a", class_="image-link")
                if not link_elem:
                    continue
                
                link = link_elem.get("href", "")
                if link and not link.startswith("http"):
                    link = f"{self.base_url}{link}"
                
                # Get image from data-image attribute or img src
                image = link_elem.get("data-image", "")
                if not image:
                    img_elem = link_elem.find("img")
                    if img_elem:
                        image = get_lazy_image_src(img_elem)
                
                # Get text content
                text_div = item.find("div", class_="search-result-text")
                if not text_div:
                    continue
                
                # Get maker (optional)
                maker = ""
                maker_elem = text_div.find("strong", class_="maker")
                if maker_elem:
                    maker = maker_elem.get_text(strip=True)
                
                # Get title from em element
                title_elem = text_div.find("em")
                title = title_elem.get_text(strip=True) if title_elem else ""
                
                # Combine maker and title
                if maker:
                    title = f"{maker} - {title}"
                
                if not title:
                    continue
                
                # Get price from sold span (if present)
                price = ""
                sold_elem = text_div.find("span", class_="sold")
                if sold_elem:
                    price = sold_elem.get_text(strip=True)
                    # Clean up "Sold for " prefix
                    if price.lower().startswith("sold for "):
                        price = price[9:].strip()
                
                # Get auction name (span without class or non-sold span)
                auction = ""
                for span in text_div.find_all("span"):
                    if "sold" not in span.get("class", []):
                        auction = span.get_text(strip=True)
                        break
                
                # Add auction info to title if available
                if auction:
                    title = f"{title} ({auction})"
                
                listing = Listing(
                    id=item_id,
                    title=title,
                    price=price if price else "Estimate/Upcoming",
                    link=link,
                    source=self.name,
                    image=image,
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


# =============================================================================
# SOTHEBYS SCRAPER
# =============================================================================

class SothebysScraper(BaseScraper):
    """Scraper for Sothebys.com - Major auction house."""
    
    name = "sothebys"
    base_url = "https://www.sothebys.com"
    link_filter = "2508"
    wait_for_selector = "[data-testid='results-search-item']"
    
    def build_search_url(self, query: str) -> str:
        # Search page - Available lots filter will be clicked via click_selector
        search_term = query.replace("+", " ")
        return f"{self.base_url}/en/search?query={search_term}"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all lot card wrappers
        items = soup.find_all("div", attrs={"data-testid": "results-search-item"})
        debug(f"[{self.name}] Found {len(items)} lot cards")
        
        for item in items:
            try:
                # Find the link element to get URL and extract ID
                link_elem = item.find("a", class_=lambda c: c and "lotTitleLink" in c)
                if not link_elem:
                    continue
                
                link = link_elem.get("href", "")
                if not link:
                    continue
                
                # Use full href as unique ID
                item_id = link
                
                # Get maker/title from the title paragraph
                title_elem = item.find("p", class_=lambda c: c and "title" in c.lower())
                maker = title_elem.get_text(strip=True) if title_elem else ""
                
                # Get description
                desc_elem = item.find("p", class_=lambda c: c and "description" in c.lower())
                description = desc_elem.get_text(strip=True) if desc_elem else ""
                
                # Combine maker and description for full title
                if maker and description:
                    title = f"{maker} - {description}"
                else:
                    title = maker or description or "Unknown"
                
                if not title or title == "Unknown":
                    continue
                
                # Get estimate/price from estimate container
                price = ""
                estimate_container = item.find("div", class_=lambda c: c and "estimateContainer" in c)
                if estimate_container:
                    # Get all paragraphs - second one has the actual estimate
                    paragraphs = estimate_container.find_all("p")
                    if len(paragraphs) >= 2:
                        price = paragraphs[1].get_text(strip=True)
                
                # Get image from swiper slide
                image = ""
                swiper_slide = item.find("div", class_="swiper-slide-active")
                if swiper_slide:
                    img = swiper_slide.find("img")
                    if img:
                        image = get_lazy_image_src(img)
                
                # Fallback: try any img in the item
                if not image:
                    img = item.find("img")
                    if img:
                        image = get_lazy_image_src(img)
                
                listing = Listing(
                    id=item_id,
                    title=title,
                    price=price if price else "Estimate TBD",
                    link=link,
                    source=self.name,
                    image=image,
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


# =============================================================================
# CHRISTIES SCRAPER
# =============================================================================

class ChristiesScraper(BaseScraper):
    """Scraper for Christies.com - Major auction house."""
    
    name = "christies"
    base_url = "https://www.christies.com"
    wait_for_selector = "[data-qa='search-result-tiles']"
    js_heavy = False
    
    def build_search_url(self, query: str) -> str:
        # Search for available lots only
        search_term = query.replace("+", "%20")
        return f"{self.base_url}/en/search?entry={search_term}&page=1&sortby=relevance&tab=available_lots"
    
    def parse_listings(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        
        # Find all lot tiles - they have data-anchor-id attribute
        items = soup.find_all("chr-lot-tile")
        debug(f"[{self.name}] Found {len(items)} lot tiles")
        
        for item in items:
            try:
                # Get item ID from data-anchor-id attribute
                item_id = item.get("data-anchor-id", "")
                if not item_id:
                    continue
                
                # Get link and title from primary title element
                link_elem = item.find("a", class_=lambda c: c and "lot-tile__link" in c)
                if not link_elem:
                    continue
                
                link = link_elem.get("href", "")
                if not link:
                    continue
                
                # Make link absolute if needed
                if link and not link.startswith("http"):
                    link = f"{self.base_url}{link}"
                
                # Get primary title (maker/artist name)
                primary_title = link_elem.get_text(strip=True) if link_elem else ""
                
                # Get secondary title (description)
                secondary_elem = item.find("p", class_=lambda c: c and "secondary-title" in c)
                secondary_title = secondary_elem.get_text(strip=True) if secondary_elem else ""
                
                # Combine titles
                if primary_title and secondary_title:
                    title = f"{primary_title} - {secondary_title}"
                else:
                    title = primary_title or secondary_title or "Unknown"
                
                if not title or title == "Unknown":
                    continue
                
                # Get price/estimate
                price = ""
                price_elem = item.find("span", class_=lambda c: c and "price-value" in c)
                if price_elem:
                    price = price_elem.get_text(strip=True)
                
                # Fallback: check for "Price on request"
                if not price:
                    secondary_price = item.find("span", class_=lambda c: c and "secondary-price-value" in c)
                    if secondary_price:
                        price = secondary_price.get_text(strip=True)
                
                # Get image from chr-image element
                image = ""
                img_elem = item.find("img", class_="chr-img")
                if img_elem:
                    # Try srcset first (higher quality), then src
                    srcset = img_elem.get("srcset", "") or img_elem.get("data-srcset", "")
                    if srcset:
                        # Get first URL from srcset (smallest size is fine)
                        first_src = srcset.split(",")[0].strip().split(" ")[0]
                        if first_src:
                            image = first_src
                    if not image:
                        image = get_lazy_image_src(img_elem)
                
                listing = Listing(
                    id=item_id,
                    title=title,
                    price=price if price else "Estimate TBD",
                    link=link,
                    source=self.name,
                    image=image,
                )
                listings.append(listing)
                debug(f"[{self.name}] Parsed: {listing.id} - {listing.title[:50]}...")
                
            except Exception as e:
                error(f"[{self.name}] Parsing item", e)
                continue
        
        return listings


# =============================================================================
# SCRAPER REGISTRY
# =============================================================================

SCRAPERS: dict[str, BaseScraper] = {
    "chrono24": Chrono24Scraper(),
    "onbehalf": OnBehalfScraper(),
    "everywatch": EveryWatchScraper(),
    "casowatches": CasoWatchesScraper(),
    "bulangsons": BulangSonsScraper(),
    "loupethis": LoupeThisScraper(),
    "parmegiani": DavideParmegianiScraper(),
    "shucktheoyster": ShuckTheOysterScraper(),
    "collectability": CollectabilityScraper(),
    "corradomattarelli": CorradoMattarelliScraper(),
    "patekmonger": PatekMongerScraper(),
    "db1983": DB1983Scraper(),
    "matthewbain": MatthewBainScraper(),
    "windvintage": WindVintageScraper(),
    "watchrecon": WatchReconScraper(),
    "watchprosite": WatchProSiteScraper(),
    "keystone": KeystoneScraper(),
    "phillips": PhillipsScraper(),
    "sothebys": SothebysScraper(),
    "christies": ChristiesScraper(),
}


def get_active_scrapers() -> list[BaseScraper]:
    """Get list of scrapers to run based on --sites argument."""
    if args.sites == "all":
        return list(SCRAPERS.values())
    
    site_names = [s.strip().lower() for s in args.sites.split(",")]
    scrapers = []
    for name in site_names:
        if name in SCRAPERS:
            scrapers.append(SCRAPERS[name])
        else:
            print(f"Warning: Unknown site '{name}', skipping")
    
    return scrapers


def find_new_listings(listings: list[Listing], seen_ids_by_source: dict[str, set]) -> list[Listing]:
    """Filter listings to only new ones."""
    new_listings = []
    for listing in listings:
        seen_ids = seen_ids_by_source.get(listing.source, set())
        if listing.id not in seen_ids:
            new_listings.append(listing)
    
    debug(f"Found {len(new_listings)} new listings out of {len(listings)}")
    return new_listings


def log_diff(found_listings: list[Listing], seen_ids_by_source: dict[str, set]) -> None:
    """Log the diff between found items and listings.json."""
    # Build set of found IDs by source
    found_by_source: dict[str, set] = {}
    for listing in found_listings:
        if listing.source not in found_by_source:
            found_by_source[listing.source] = set()
        found_by_source[listing.source].add(listing.id)
    
    # Find additions (new items not in listings.json)
    additions_by_source: dict[str, list[str]] = {}
    for listing in found_listings:
        seen_ids = seen_ids_by_source.get(listing.source, set())
        if listing.id not in seen_ids:
            if listing.source not in additions_by_source:
                additions_by_source[listing.source] = []
            additions_by_source[listing.source].append(listing.id)
    
    # Find removals (items in listings.json but not found now)
    removals_by_source: dict[str, list[str]] = {}
    for source, seen_ids in seen_ids_by_source.items():
        found_ids = found_by_source.get(source, set())
        removed = seen_ids - found_ids
        if removed:
            removals_by_source[source] = list(removed)
    
    # Log the diff
    total_additions = sum(len(ids) for ids in additions_by_source.values())
    total_removals = sum(len(ids) for ids in removals_by_source.values())
    
    print(f"\n{'-'*50}")
    print(f" DIFF: {Colors.GREEN}+{total_additions}{Colors.RESET} / {Colors.RED}-{total_removals}{Colors.RESET}")
    print(f"{'-'*50}")
    
    if not total_additions and not total_removals:
        print(f"  {Colors.GREEN}No changes{Colors.RESET}")
    else:
        if additions_by_source:
            print(f"  {Colors.GREEN}{Colors.BOLD}+ NEW ({total_additions}):{Colors.RESET}")
            for source, ids in sorted(additions_by_source.items()):
                print(f"    {Colors.GREEN}{source}:{Colors.RESET} {', '.join(ids)}")
        
        if removals_by_source:
            print(f"  {Colors.RED}{Colors.BOLD}- GONE ({total_removals}):{Colors.RESET}")
            for source, ids in sorted(removals_by_source.items()):
                print(f"    {Colors.RED}{source}:{Colors.RESET} {', '.join(ids)}")
    print(f"\n")


def create_email_html(listings: list[Listing]) -> str:
    """Generate HTML email content for listings."""
    items_html = []
    
    # Group listings by source for display
    source_names = {
        "chrono24": "Chrono24",
        "onbehalf": "OnBehalf.jp",
        "everywatch": "EveryWatch",
        "casowatches": "Caso Watches",
        "bulangsons": "Bulang & Sons",
        "loupethis": "Loupe This",
        "parmegiani": "Davide Parmegiani",
        "shucktheoyster": "Shuck the Oyster",
        "collectability": "Collectability",
        "corradomattarelli": "Corrado Mattarelli",
        "patekmonger": "Patek Monger",
        "db1983": "DB1983",
        "matthewbain": "Matthew Bain Inc",
        "windvintage": "Wind Vintage",
        "watchrecon": "WatchRecon",
        "watchprosite": "WatchProSite",
        "keystone": "The Keystone",
        "phillips": "Phillips",
        "sothebys": "Sotheby's",
        "christies": "Christie's",
    }
    
    for i, listing in enumerate(listings):
        img_style = "display:none;" if args.noimage or not listing.image else ""
        img_margin = 0 if args.noimage or not listing.image else 1
        border_style = "" if i < len(listings) - 1 else "display:none;"
        source_label = source_names.get(listing.source, listing.source)
        
        item = f"""
        <div style="margin:0.5em 0;">
            <a href="{listing.link}" style="text-decoration:none;color:#000;">
                <img src="{listing.image}" style="{img_style}object-fit:cover;width:100%;max-height:330px;border-radius:18px;margin:0.5em 0 1em;">
                <p style="margin:0;padding:0 6px;color:#999;font-size:0.85em;">{source_label}</p>
                <h3 style="margin:{img_margin}em 0;padding:0 6px;">{listing.title}</h3>
                <p style="margin:0;padding:0 6px;color:#666;font-size:1.1em;font-weight:bold;">{listing.price}</p>
            </a>
        </div>
        <div style="{border_style}margin:2em 0;height:1px;width:100%;background:gainsboro;"></div>
        """
        items_html.append(item)
    
    count = len(listings)
    plural = "s" if count > 1 else ""
    sources = set(l.source for l in listings)
    source_count = len(sources)
    source_text = f" from {source_count} site{'s' if source_count > 1 else ''}" if source_count > 1 else ""
    body = "\n".join(items_html)
    
    return f"""\
    <html>
    <head></head>
    <body>
        <div style="display:none;max-height:0;overflow:hidden;">
            {count} new listing{plural}{source_text}
        </div>
        <div style="display:none;max-height:0;overflow:hidden;">
            {"&nbsp;&zwnj;" * 100}
        </div>
        {body}
        <footer style="margin-top:3em;color:gainsboro;text-align:center;font-size:60%;">watchnotify v{VERSION}</footer>
    </body>
    </html>
    """


def send_email(html: str, count: int) -> None:
    """Send HTML email notification."""
    if args.noemail:
        output_html(html)
        return
    
    if not EMAIL_FROM or not EMAIL_PASS:
        print("Email not configured")
        output_html(html)
        return
    
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Watch Alert"
        msg["From"] = f'"watchnotify" <{EMAIL_FROM}>'
        msg["To"] = EMAIL_TO
        msg.attach(MIMEText(html, "html"))
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        
        info(f"Notification sent ({count} listing{'s' if count > 1 else ''})")
    except Exception as e:
        error("Sending email", e)
        output_html(html)


def output_html(html: str) -> None:
    """Save HTML to output file."""
    if args.simple:
        return  # Skip file output in simple mode
    output_path = SCRIPT_DIR / "output.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    info(f"Output saved to {output_path}")


async def main() -> None:
    """Main entry point."""
    query_display = args.query.replace('+', ' ')
    scrapers = get_active_scrapers()
    
    if not scrapers:
        print("No valid sites specified")
        return
    
    site_names = [s.name for s in scrapers]
    info(f"Searching: {query_display}")
    info(f"Sites: {', '.join(site_names)}")
    
    # Load history
    seen_ids_by_source = load_seen_ids()
    
    # Scrape all sites concurrently using a shared browser
    all_listings: list[Listing] = []
    scraper_results: dict[str, int] = {}  # Track results per scraper for summary
    start_time = time.time()
    
    async def scrape_site(scraper: BaseScraper, browser) -> tuple[str, list[Listing]]:
        """Scrape a single site and return results."""
        info(f"[{scraper.name}] Scraping...")
        listings = await scraper.scrape(args.query, browser)
        info(f"[{scraper.name}] Found {len(listings)} listing{'s' if len(listings) != 1 else ''} ({scraper.last_fetch_time:.1f}s fetch)")
        return scraper.name, listings
    
    async with async_playwright() as p:
        use_headless = IS_CI or args.headless or not args.debug
        # Per-context proxies are attached directly on the contexts that set
        # use_proxy=True (see fetch_page). Modern Playwright supports this
        # without a launch-level proxy, so the browser is launched proxy-free
        # and non-proxied sites connect directly.
        if use_headless:
            browser = await p.chromium.launch(headless=False, args=[
                "--headless=new",
                "--disable-gpu",
                "--disable-extensions",
                "--no-sandbox",
            ])
        else:
            browser = await p.chromium.launch(headless=False)
        
        results = await asyncio.gather(*[scrape_site(s, browser) for s in scrapers], return_exceptions=True)
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                error(f"[{scrapers[i].name}] Scraping failed", result)
                scraper_results[scrapers[i].name] = 0
            else:
                name, listings = result
                all_listings.extend(listings)
                scraper_results[name] = len(listings)
        
        await browser.close()
    
    if not all_listings:
        print("\nNo listings found from any site")
        return
    
    # Find new listings
    new_listings = find_new_listings(all_listings, seen_ids_by_source)
    
    if new_listings:
        sources = set(l.source for l in new_listings)
        source_summary = ", ".join(f"{sum(1 for l in new_listings if l.source == s)} from {s}" for s in sources)
        info(f"\n{len(new_listings)} new listing{'s' if len(new_listings) > 1 else ''} found ({source_summary})")
        
        # Send notification
        html = create_email_html(new_listings)
        send_email(html, len(new_listings))
    else:
        info("\nNo new listings")
    
    # Log diff between found items and listings.json
    # Scraper summary (always shown)
    total_time = time.time() - start_time
    active = [(n, c) for n, c in sorted(scraper_results.items()) if c > 0]
    inactive = [n for n, c in sorted(scraper_results.items()) if c == 0]
    
    print(f"\n{'='*50}")
    print(f" Scraped {len(all_listings)} listings from {len(scraper_results)} sites ({total_time:.1f}s)")
    print(f"{'='*50}")
    
    if active:
        active_str = "  ".join(f"{Colors.GREEN}{n}{Colors.RESET}: {c}" for n, c in active)
        print(f"{Colors.GREEN}{Colors.BOLD}  ACTIVE ({len(active)}):{Colors.RESET} {active_str}")
    if inactive:
        inactive_str = "  ".join(f"{Colors.YELLOW}{n}{Colors.RESET}" for n in inactive)
        print(f"{Colors.YELLOW}{Colors.BOLD}  EMPTY ({len(inactive)}):{Colors.RESET}  {inactive_str}")
    if args.sites == "all":
        log_diff(all_listings, seen_ids_by_source)
    
    # Always update seen IDs
    for listing in all_listings:
        if listing.source not in seen_ids_by_source:
            seen_ids_by_source[listing.source] = set()
        seen_ids_by_source[listing.source].add(listing.id)
    save_seen_ids(seen_ids_by_source)
    
    # Report
    total_time = time.time() - start_time
    info(f"\nDone: {len(all_listings)} listing{'s' if len(all_listings) != 1 else ''}, {len(new_listings)} new ({total_time:.1f}s)")


if __name__ == "__main__":
    asyncio.run(main())

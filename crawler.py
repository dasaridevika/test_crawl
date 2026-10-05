"""
Production-Grade Web Crawler & Structured Engine
=================================================
Powered by Crawlee for Python + PlaywrightCrawler (Headless Chromium).

Features:
- Self-contained single crawler abstraction compatible with both local & Streamlit Cloud (Linux)
- 100% complete exact content extraction (zero truncation, no image clutter, no cookie banners)
- Multi-representation: Rendered HTML, clean Markdown, and pure plain text
- Dynamic robots.txt parsing and Crawl-delay politeness rate limiting
- Automatic XML sitemap discovery (.xml, .xml.gz, sitemap_index.xml)
- Strict SSRF protection (allowing public dual-stack NAT64 CDN routes)
- Linux / Docker container sandbox compatibility (--no-sandbox, --disable-dev-shm-usage)
"""

import argparse
import asyncio
import gzip
import html as html_lib
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request as URLRequest, urlopen

import defusedxml.ElementTree as ET
from bs4 import BeautifulSoup, Comment
from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.storages import RequestQueue
import markdownify

# -----------------------------------------------------------------------------
# LOGGING CONFIGURATION
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("CrawleeCrawlerEngine")


# -----------------------------------------------------------------------------
# AUTO-INSTALL PLAYWRIGHT BROWSERS IF MISSING (Streamlit Cloud Compatibility)
# -----------------------------------------------------------------------------
def ensure_playwright_browsers():
    """Ensures Chromium binaries are installed when running in Linux / Streamlit Cloud."""
    try:
        if sys.platform != "win32":
            cache_dir = os.path.expanduser("~/.cache/ms-playwright")
            if not os.path.exists(cache_dir) or not os.listdir(cache_dir):
                logger.info("Installing Playwright Chromium browser binaries...")
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    except Exception as e:
        logger.warning(f"Note on playwright install check: {e}")


ensure_playwright_browsers()


# -----------------------------------------------------------------------------
# SSRF & URL SECURITY VALIDATION
# -----------------------------------------------------------------------------
DISALLOWED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "metadata.google.internal",
    "instance-data",
}

DISALLOWED_IPS = {
    "169.254.169.254",  # AWS/GCP/Azure link-local metadata
    "100.100.100.200",  # Alibaba cloud metadata
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def is_safe_url(url: str) -> Tuple[bool, Optional[str]]:
    """
    Validates URL scheme and resolves DNS to ensure requests are not directed
    at private IP ranges, loopback interfaces, or cloud metadata services.
    Allows public global IPv6 NAT64 translation addresses.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"Disallowed scheme: {parsed.scheme}"

        hostname = parsed.hostname
        if not hostname:
            return False, "Missing hostname in URL"

        hostname_lower = hostname.lower()
        if (
            hostname_lower in DISALLOWED_HOSTS
            or hostname_lower.endswith(".local")
            or hostname_lower.endswith(".internal")
        ):
            return False, f"Disallowed internal host: {hostname}"

        if hostname_lower in DISALLOWED_IPS:
            return False, f"Disallowed cloud metadata IP: {hostname}"

        # Resolve IP to detect SSRF against private / loopback IP ranges
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for family, _, _, _, sockaddr in addr_info:
                ip_str = sockaddr[0]
                ip = ipaddress.ip_address(ip_str)

                if ip_str in DISALLOWED_IPS:
                    return False, f"Resolved to cloud metadata address: {ip_str}"

                if (
                    ip.is_loopback
                    or ip.is_private
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_unspecified
                ):
                    return False, f"Resolved to disallowed IP: {ip_str}"
        except socket.gaierror:
            pass

        return True, None
    except Exception as e:
        return False, f"URL security validation error: {str(e)}"


# -----------------------------------------------------------------------------
# ACCESS CHALLENGE PATTERNS
# -----------------------------------------------------------------------------
ACCESS_CHALLENGE_PATTERNS = [
    re.compile(r"attention required!\s*\|\s*cloudflare", re.I),
    re.compile(r"just a moment\.\.\.", re.I),
    re.compile(r"cf-turnstile|cf-challenge", re.I),
    re.compile(r"ddos-guard", re.I),
    re.compile(r"please verify you are a human", re.I),
    re.compile(r"access to this page has been denied", re.I),
    re.compile(r"px-captcha|perimeterx", re.I),
    re.compile(r"incapsula_resource", re.I),
]


def detect_access_challenge(status_code: Optional[int], html_content: str) -> Tuple[bool, Optional[str]]:
    """Checks if response indicates an access challenge / bot block."""
    if status_code in (401, 403):
        return True, f"http_{status_code}_access_denied"
    if status_code == 429:
        return True, "http_429_rate_limited"

    for pattern in ACCESS_CHALLENGE_PATTERNS:
        if pattern.search(html_content):
            return True, "access_challenge_detected"
    return False, None


# -----------------------------------------------------------------------------
# URL NORMALIZATION & SCOPE MATCHING
# -----------------------------------------------------------------------------
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "dclid", "ncid", "session_id", "trk", "_ga", "mc_eid",
    "ref", "source", "ref_src", "nvid", "cmpid", "cid"
}


def get_apex_domain(hostname: str) -> str:
    """Extracts apex domain (e.g., 'nvidia.com' from 'blogs.nvidia.com')."""
    parts = hostname.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return hostname.lower()


def normalize_url(raw_url: str, base_url: str) -> Optional[str]:
    """
    Normalizes a URL for deterministic deduplication:
    - Resolves relative links
    - Drops fragments (#...)
    - Lowercases scheme and host
    - Strips default ports (:80, :443)
    - Strips marketing tracking parameters (utm_*, gclid, nvid, etc.)
    - Sorts remaining query parameters canonically
    - Collapses duplicate slashes
    """
    if not raw_url or raw_url.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "blob:")):
        return None

    try:
        full_url = urljoin(base_url, raw_url).split("#")[0].strip()
        parsed = urlparse(full_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None

        # Clean host & port
        hostname = parsed.hostname.lower()
        port = parsed.port
        if (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
            netloc = hostname
        elif port:
            netloc = f"{hostname}:{port}"
        else:
            netloc = hostname

        # Clean and sort query parameters
        if parsed.query:
            qs = parse_qs(parsed.query, keep_blank_values=True)
            filtered_qs = {k: v for k, v in sorted(qs.items()) if k.lower() not in TRACKING_PARAMS}
            clean_query = urlencode(filtered_qs, doseq=True) if filtered_qs else ""
        else:
            clean_query = ""

        # Normalize path
        clean_path = parsed.path
        while "//" in clean_path:
            clean_path = clean_path.replace("//", "/")
        if clean_path != "/" and clean_path.endswith("/"):
            clean_path = clean_path.rstrip("/")

        return urlunparse((parsed.scheme.lower(), netloc, clean_path, "", clean_query, ""))
    except Exception:
        return None


def is_in_scope(target_url: str, seed_url: str, allow_subdomains: bool = False) -> bool:
    """Enforces domain boundaries (exact host or apex domain)."""
    try:
        target_parsed = urlparse(target_url)
        seed_parsed = urlparse(seed_url)

        if not target_parsed.hostname or not seed_parsed.hostname:
            return False

        target_host = target_parsed.hostname.lower()
        seed_host = seed_parsed.hostname.lower()

        if not allow_subdomains:
            return target_host == seed_host

        return get_apex_domain(target_host) == get_apex_domain(seed_host)
    except Exception:
        return False


# -----------------------------------------------------------------------------
# ROBOTS.TXT & POLITENESS MANAGER
# -----------------------------------------------------------------------------
class RobotsManager:
    """Fetches and parses robots.txt to enforce path permissions and crawl delays."""

    def __init__(self, seed_url: str, user_agent: str, timeout: int = 10):
        self.seed_url = seed_url
        self.user_agent = user_agent
        self.timeout = timeout
        self.parser = urllib.robotparser.RobotFileParser()
        self.sitemaps: List[str] = []
        self._fetched = False

    def fetch(self) -> None:
        if self._fetched:
            return

        parsed = urlparse(self.seed_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        safe, reason = is_safe_url(robots_url)
        if not safe:
            self._fetched = True
            return

        try:
            req = URLRequest(robots_url, headers={"User-Agent": self.user_agent})
            with urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    content = resp.read().decode("utf-8", errors="replace")
                    self.parser.parse(content.splitlines())
                    for line in content.splitlines():
                        if line.strip().lower().startswith("sitemap:"):
                            s_url = line.split(":", 1)[1].strip()
                            if s_url and s_url not in self.sitemaps:
                                self.sitemaps.append(s_url)
        except Exception:
            self.parser.allow_all = True
        finally:
            self._fetched = True

    def can_fetch(self, url: str) -> bool:
        if not self._fetched:
            self.fetch()
        try:
            return self.parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def get_crawl_delay(self) -> Optional[float]:
        if not self._fetched:
            self.fetch()
        try:
            delay = self.parser.crawl_delay(self.user_agent)
            return float(delay) if delay is not None else None
        except Exception:
            return None


# -----------------------------------------------------------------------------
# SITEMAP DISCOVERER
# -----------------------------------------------------------------------------
class SitemapDiscoverer:
    """Discovers in-scope URLs from sitemap.xml and nested sitemap index feeds."""

    def __init__(self, seed_url: str, user_agent: str, allow_subdomains: bool = False, max_sitemaps: int = 5, max_urls: int = 1000):
        self.seed_url = seed_url
        self.user_agent = user_agent
        self.allow_subdomains = allow_subdomains
        self.max_sitemaps = max_sitemaps
        self.max_urls = max_urls

    def discover(self, declared_sitemaps: Optional[List[str]] = None) -> List[str]:
        parsed = urlparse(self.seed_url)
        base_origin = f"{parsed.scheme}://{parsed.netloc}"

        candidate_sitemaps: List[str] = list(declared_sitemaps or [])
        default_sitemaps = [f"{base_origin}/sitemap.xml", f"{base_origin}/sitemap_index.xml"]
        for s in default_sitemaps:
            if s not in candidate_sitemaps:
                candidate_sitemaps.append(s)

        discovered_urls: Set[str] = set()
        visited_sitemaps: Set[str] = set()
        queue: List[str] = list(candidate_sitemaps)

        while queue and len(visited_sitemaps) < self.max_sitemaps and len(discovered_urls) < self.max_urls:
            s_url = queue.pop(0)
            if s_url in visited_sitemaps:
                continue
            visited_sitemaps.add(s_url)

            safe, _ = is_safe_url(s_url)
            if not safe:
                continue

            try:
                req = URLRequest(s_url, headers={"User-Agent": self.user_agent})
                with urlopen(req, timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    raw_bytes = resp.read()

                    if s_url.endswith(".gz") or raw_bytes[:2] == b"\x1f\x8b":
                        try:
                            raw_bytes = gzip.decompress(raw_bytes)
                        except Exception:
                            continue

                    try:
                        root = ET.fromstring(raw_bytes)
                        for sitemap_node in root.findall(".//{*}sitemap"):
                            loc_node = sitemap_node.find("{*}loc")
                            if loc_node is not None and loc_node.text:
                                sub_s_url = loc_node.text.strip()
                                if sub_s_url not in visited_sitemaps and len(queue) < self.max_sitemaps:
                                    queue.append(sub_s_url)

                        for url_node in root.findall(".//{*}url"):
                            loc_node = url_node.find("{*}loc")
                            if loc_node is not None and loc_node.text:
                                cand_url = normalize_url(loc_node.text.strip(), self.seed_url)
                                if cand_url and is_in_scope(cand_url, self.seed_url, self.allow_subdomains):
                                    discovered_urls.add(cand_url)
                                    if len(discovered_urls) >= self.max_urls:
                                        break
                    except Exception:
                        pass
            except Exception:
                pass

        return sorted(list(discovered_urls))


# -----------------------------------------------------------------------------
# EXACT CONTENT EXTRACTOR (No Image Clutter, No Cookie Banners)
# -----------------------------------------------------------------------------
def clean_whitespace(text: str) -> str:
    """Normalizes whitespace while preserving paragraph and heading layout."""
    lines = [line.strip() for line in text.splitlines()]
    cleaned_lines = []
    prev_empty = False
    for line in lines:
        if not line:
            if not prev_empty:
                cleaned_lines.append("")
                prev_empty = True
        else:
            cleaned_lines.append(line)
            prev_empty = False
    return "\n".join(cleaned_lines).strip()


def extract_exact_content(rendered_html: str, url: str) -> Dict[str, Any]:
    """
    Extracts 100% complete editorial content without image clutter,
    cookie consent banners, or broken anchor links.
    """
    if not rendered_html or not rendered_html.strip():
        return {
            "url": url,
            "title": "Empty Page",
            "meta_description": "",
            "word_count": 0,
            "character_count": 0,
            "markdown": "",
            "plain_text": "",
            "rendered_html": ""
        }

    raw_soup = BeautifulSoup(rendered_html, "html.parser")
    title_tag = raw_soup.find("title")
    h1_tag = raw_soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else (h1_tag.get_text(" ", strip=True) if h1_tag else "Untitled")
    title = re.sub(r"\s+", " ", title).strip()

    meta_description = ""
    meta_desc_tag = raw_soup.find("meta", attrs={"name": re.compile(r"description", re.I)}) or \
                    raw_soup.find("meta", attrs={"property": re.compile(r"og:description", re.I)})
    if meta_desc_tag and meta_desc_tag.get("content"):
        meta_description = meta_desc_tag["content"].strip()

    # Build clean DOM tree
    content_soup = BeautifulSoup(rendered_html, "html.parser")

    # Decompose non-content, media, forms, and layout shells
    for tag in content_soup(["script", "style", "noscript", "svg", "img", "picture", "source", "canvas", "video", "audio", "iframe", "button", "input", "select", "option", "form", "header", "nav", "footer"]):
        tag.decompose()

    # Remove HTML comments
    for comment in content_soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Comprehensive boilerplate removal: mega-menus, drawers, banners, indicators, footers
    boilerplate_selectors = [
        # Navigation & Mega-menus
        "[role='navigation']", "[role='contentinfo']", "[role='banner']",
        ".mega-menu", ".global-nav", ".navbar", ".navigation", ".site-header", ".site-footer",
        ".drawer-menu", ".mobile-menu", ".gn-header", ".header-container", ".main-nav",
        # Cookie Banners & Consent Modals
        "#onetrust-consent-sdk", "#onetrust-banner-sdk", "#onetrust-pc-sdk",
        ".optanon-alert-box-wrapper", ".cookie-banner", ".ot-sdk-container",
        ".region-selector-modal", ".country-selector", ".region-banner", ".country-banner", ".locale-banner",
        ".modal-backdrop", "[id*='cookie']", "[id*='consent']", "[class*='cookie']", "[class*='consent']",
        "[role='dialog']", "[role='alertdialog']", "[aria-modal='true']",
        # Accessibility skip links
        ".skip-to-content", ".skip-link", "a[href*='#main']", "a[href*='#content']",
        # Carousel Indicators & Thumbnail Labels
        ".cmp-carousel__indicators", ".cmp-carousel__actions", ".cmp-carousel__indicator",
        ".carousel-indicators", ".swiper-pagination", ".slick-dots", ".slider-nav",
        # Footer link directories & Copyright blocks
        "#globalFooter", ".global-footer", ".global-footer-container",
        ".page-footer", ".page-footer__links", ".page-footer-link-set",
        "[class*='global-footer']", "[class*='site-footer']", "[class*='page-footer']",
        "[id*='globalFooter']", "[id*='footer']"
    ]
    for sel in boilerplate_selectors:
        try:
            for el in content_soup.select(sel):
                el.decompose()
        except Exception:
            pass

    body = content_soup.find("body") or content_soup

    # Clean links: Keep real destinations, remove empty '#' or javascript links
    for a in body.find_all("a"):
        href = a.get("href", "").strip()
        link_text = a.get_text(" ", strip=True)
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")) or not link_text:
            a.unwrap()
        else:
            abs_url = urljoin(url, href)
            a["href"] = abs_url

    cleaned_html = str(body)
    
    markdown_content = markdownify.markdownify(
        cleaned_html,
        heading_style="ATX",
        bullets_style="-",
        autolinks=False,
        strip=["img", "picture", "svg", "canvas", "script", "style"]
    )
    markdown_content = clean_whitespace(markdown_content)

    plain_text = body.get_text(separator="\n", strip=True)
    plain_text = clean_whitespace(plain_text)

    word_count = len(re.findall(r"\w+", plain_text))
    character_count = len(plain_text)

    return {
        "url": url,
        "title": title,
        "meta_description": meta_description,
        "word_count": word_count,
        "character_count": character_count,
        "markdown": markdown_content,
        "plain_text": plain_text,
        "rendered_html": rendered_html
    }


# -----------------------------------------------------------------------------
# CRAWL CONFIGURATION
# -----------------------------------------------------------------------------
@dataclass
class CrawlConfig:
    seed_url: str
    max_pages: int = 25
    max_depth: int = 2
    concurrency: int = 2
    timeout: int = 30
    retries: int = 1
    delay: float = 0.3
    allow_subdomains: bool = False
    respect_robots: bool = True
    discover_sitemaps: bool = True
    crawl_mode: str = "browser"  # "browser" (Playwright), "turbo" (Adaptive Fast), "http" (Fast HTTP)
    user_agent: str = USER_AGENT


# -----------------------------------------------------------------------------
# JINA READER (r.jina.ai) FETCHER & FAST PARSER
# -----------------------------------------------------------------------------
def fetch_jina_reader(url: str, api_key: Optional[str] = None, timeout: int = 30) -> Dict[str, Any]:
    """Fetches clean Markdown and metadata using Jina Reader (r.jina.ai)."""
    import ssl
    jina_endpoint = f"https://r.jina.ai/{url}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = URLRequest(jina_endpoint, headers=headers)

    try:
        with urlopen(req, timeout=timeout) as response:
            status_code = response.getcode()
            raw_data = response.read()
            encoding = response.info().get("Content-Encoding", "").lower()
            if "gzip" in encoding:
                raw_data = gzip.decompress(raw_data)
            payload = json.loads(raw_data.decode("utf-8", errors="replace"))
            data = payload.get("data", {})
            
            title = data.get("title") or "Untitled"
            desc = data.get("description") or ""
            markdown_content = data.get("content") or ""
            final_url = data.get("url") or url

            # Generate plain text from markdown
            plain_text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", markdown_content)
            plain_text = re.sub(r"[#*`_~>-]", " ", plain_text)
            plain_text = clean_whitespace(plain_text)
            word_count = len(re.findall(r"\w+", plain_text))

            # Extract discovered links for crawling
            discovered_links = []
            for match in re.findall(r"\[(?:[^\]]*)\]\((https?://[^\)\s]+)\)", markdown_content):
                norm = normalize_url(match, final_url)
                if norm:
                    discovered_links.append(norm)

            return {
                "status": "SUCCESS" if status_code == 200 else "ERROR",
                "http_status": status_code,
                "title": title,
                "meta_description": desc,
                "word_count": word_count,
                "character_count": len(plain_text),
                "markdown": markdown_content,
                "plain_text": plain_text,
                "rendered_html": f"<pre>{html_lib.escape(markdown_content)}</pre>",
                "links": discovered_links,
                "final_url": final_url,
                "error": None
            }
    except Exception as e:
        logger.warning(f"Jina Reader fetch error for {url}: {e}")
        return {
            "status": "ERROR",
            "http_status": 0,
            "title": "Crawl Error",
            "meta_description": "",
            "word_count": 0,
            "character_count": 0,
            "markdown": "",
            "plain_text": "",
            "rendered_html": "",
            "links": [],
            "final_url": url,
            "error": {"code": "jina_error", "message": str(e)}
        }


# -----------------------------------------------------------------------------
# PLAYWRIGHT BINARY ENSURER & RESILIENT HTTP FALLBACK
# -----------------------------------------------------------------------------
_PLAYWRIGHT_CHECKED = False

def ensure_playwright_browsers():
    """Ensure Chromium browser binaries are installed for Playwright in headless/cloud environments."""
    global _PLAYWRIGHT_CHECKED
    if _PLAYWRIGHT_CHECKED:
        return
    _PLAYWRIGHT_CHECKED = True
    try:
        # Non-blocking attempt to install playwright chromium if needed
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45
        )
    except Exception as e:
        logger.debug(f"Playwright install check note: {e}")


def fetch_html_fallback(url: str, user_agent: str = USER_AGENT, timeout: int = 15) -> Tuple[int, str, str]:
    """Resilient HTTP fetcher fallback with gzip/deflate support and standard browser headers."""
    import ssl
    import zlib
    import urllib.error
    
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive"
    }
    req = URLRequest(url, headers=headers)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urlopen(req, timeout=timeout, context=ctx) as response:
            status_code = response.getcode()
            final_url = response.geturl()
            raw_data = response.read()
            encoding = response.info().get("Content-Encoding", "").lower()
            if "gzip" in encoding:
                raw_data = gzip.decompress(raw_data)
            elif "deflate" in encoding:
                try:
                    raw_data = zlib.decompress(raw_data)
                except Exception:
                    raw_data = zlib.decompress(raw_data, -zlib.MAX_WBITS)

            charset = response.headers.get_content_charset() or "utf-8"
            html_text = raw_data.decode(charset, errors="replace")
            return status_code, html_text, final_url
    except urllib.error.HTTPError as e:
        return e.code, "", url
    except Exception as e:
        logger.warning(f"HTTP fallback error on {url}: {e}")
        return 0, "", url


async def render_single_page_playwright(url: str, user_agent: str = USER_AGENT, timeout: int = 15) -> Tuple[int, str, str]:
    """Fast single-page headless Chromium render with heavy media/tracker blocking."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = await browser.new_context(user_agent=user_agent)
            page = await context.new_page()

            async def route_filter(route):
                if route.request.resource_type in ["image", "media", "font"]:
                    await route.abort()
                elif any(t in route.request.url.lower() for t in ["google-analytics", "doubleclick", "facebook.net", "googletagmanager", "hotjar"]):
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", route_filter)
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            status_code = resp.status if resp else 200
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await asyncio.sleep(0.1)
            except Exception:
                pass
            content = await page.content()
            final_url = page.url or url
            await browser.close()
            return status_code, content, final_url
    except Exception as e:
        logger.warning(f"Playwright single render error for {url}: {e}")
        return 0, "", url


# -----------------------------------------------------------------------------
# CRAWLEE PLAYWRIGHT CRAWLER ENGINE
# -----------------------------------------------------------------------------
class CrawleeWebCrawler:
    """Production multi-page crawler engine built on Crawlee PlaywrightCrawler."""

    def __init__(self, config: CrawlConfig):
        self.config = config
        self.robots = RobotsManager(config.seed_url, config.user_agent)
        self.sitemaps = SitemapDiscoverer(config.seed_url, config.user_agent, allow_subdomains=config.allow_subdomains)
        self.seen_urls: Set[str] = set()
        self.results: List[Dict[str, Any]] = []
        self.stats: Dict[str, Any] = {
            "attempted": 0,
            "successful": 0,
            "failed": 0,
            "blocked": 0,
            "total_words": 0,
            "total_bytes": 0,
            "elapsed_seconds": 0.0,
            "crawl_completion_status": "RUNNING",
            "completion_message": ""
        }

    async def crawl(self, on_page_crawled: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None) -> List[Dict[str, Any]]:
        start_time = time.perf_counter()
        logger.info(f"Starting Crawl on {self.config.seed_url} (mode={self.config.crawl_mode}, max_pages={self.config.max_pages}, max_depth={self.config.max_depth})")
        ensure_playwright_browsers()

        # 1. Security check
        safe, reason = is_safe_url(self.config.seed_url)
        if not safe:
            err_record = {
                "url": self.config.seed_url,
                "final_url": None,
                "status": "ERROR_SECURITY",
                "http_status": None,
                "title": "Security Error",
                "word_count": 0,
                "character_count": 0,
                "markdown": "",
                "plain_text": "",
                "rendered_html": "",
                "error": {"code": "security_block", "message": f"Target URL rejected by security policy: {reason}"},
                "depth": 0,
                "fetch_time_ms": 0.0
            }
            self.results.append(err_record)
            self.stats["failed"] = 1
            self.stats["crawl_completion_status"] = "ERROR_SECURITY"
            self.stats["completion_message"] = f"Crawl blocked by security policy: {reason}"
            if on_page_crawled:
                try:
                    on_page_crawled(err_record, self.stats)
                except Exception:
                    pass
            return self.results

        # 2. Robots.txt check & delay
        effective_delay = self.config.delay
        if self.config.respect_robots:
            self.robots.fetch()
            r_delay = self.robots.get_crawl_delay()
            if r_delay is not None and r_delay > effective_delay:
                effective_delay = min(r_delay, 10.0)
                logger.info(f"Applying robots.txt Crawl-delay: {effective_delay}s")

        # 3. Discover Sitemaps
        discovered_sitemaps: List[str] = []
        if self.config.discover_sitemaps:
            discovered_sitemaps = self.sitemaps.discover(self.robots.sitemaps)

        # 4. Route to Jina Reader or Fast Hybrid Engine
        if self.config.crawl_mode == "jina":
            return await self._crawl_jina(on_page_crawled, start_time, effective_delay, discovered_sitemaps)

        if self.config.crawl_mode in ["turbo", "http"]:
            return await self._crawl_fast(on_page_crawled, start_time, effective_delay, discovered_sitemaps)

        # 5. Initialize Unique RequestQueue for Full Browser Mode
        queue_name = f"crawl-queue-{int(time.time() * 1000)}"
        request_queue = await RequestQueue.open(name=queue_name)
        norm_seed = normalize_url(self.config.seed_url, self.config.seed_url) or self.config.seed_url
        self.seen_urls.add(norm_seed)
        await request_queue.add_request(Request.from_url(norm_seed, user_data={"depth": 0}))

        for s_url in discovered_sitemaps:
            if s_url not in self.seen_urls and len(self.seen_urls) < self.config.max_pages * 3:
                self.seen_urls.add(s_url)
                await request_queue.add_request(Request.from_url(s_url, user_data={"depth": 1}))

        # 6. Initialize Crawlee PlaywrightCrawler with Linux sandbox flags & resource route blocking
        concurrency = max(1, self.config.concurrency)
        crawler = PlaywrightCrawler(
            request_manager=request_queue,
            max_requests_per_crawl=self.config.max_pages,
            max_request_retries=self.config.retries,
            request_handler_timeout=timedelta(seconds=self.config.timeout),
            navigation_timeout=timedelta(seconds=self.config.timeout),
            concurrency_settings=ConcurrencySettings(
                min_concurrency=1,
                max_concurrency=concurrency,
                desired_concurrency=concurrency
            ),
            headless=True,
            browser_launch_options={
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu"
                ]
            },
            browser_new_context_options={"user_agent": self.config.user_agent},
            goto_options={"wait_until": "domcontentloaded"}
        )

        @crawler.router.default_handler
        async def request_handler(context: PlaywrightCrawlingContext) -> None:
            current_url = getattr(getattr(context, "request", None), "url", "")
            user_data = getattr(getattr(context, "request", None), "user_data", {}) or {}
            depth = user_data.get("depth", 0)
            self.stats["attempted"] += 1

            if effective_delay > 0:
                await asyncio.sleep(effective_delay)

            if self.config.respect_robots and not self.robots.can_fetch(current_url):
                rec = {
                    "url": current_url,
                    "final_url": None,
                    "status": "SKIPPED_ROBOTS",
                    "http_status": None,
                    "title": "Disallowed by robots.txt",
                    "word_count": 0,
                    "character_count": 0,
                    "markdown": "",
                    "plain_text": "",
                    "rendered_html": "",
                    "error": None,
                    "depth": depth,
                    "fetch_time_ms": 0.0
                }
                self.results.append(rec)
                if on_page_crawled:
                    try:
                        on_page_crawled(rec, self.stats)
                    except Exception:
                        pass
                return

            t0 = time.perf_counter()
            page = getattr(context, "page", None)
            rendered_html = ""
            final_url = current_url

            if page:
                try:
                    async def route_filter(route):
                        if route.request.resource_type in ["image", "media", "font"]:
                            await route.abort()
                        elif any(t in route.request.url.lower() for t in ["google-analytics", "doubleclick", "facebook.net", "googletagmanager", "hotjar"]):
                            await route.abort()
                        else:
                            await route.continue_()
                    await page.route("**/*", route_filter)
                except Exception:
                    pass

                try:
                    await page.wait_for_load_state("domcontentloaded")
                    await page.evaluate("""async () => {
                        window.scrollTo(0, document.body.scrollHeight / 2);
                        await new Promise(r => setTimeout(r, 100));
                        window.scrollTo(0, 0);
                    }""")
                except Exception:
                    pass

                try:
                    rendered_html = await page.content()
                    final_url = page.url or current_url
                except Exception:
                    rendered_html = ""

            resp = getattr(context, "response", None)
            http_status = getattr(resp, "status", None) or 200
            fetch_time_ms = round((time.perf_counter() - t0) * 1000.0, 2)

            extracted = extract_exact_content(rendered_html, final_url)
            is_blocked, marker = detect_access_challenge(http_status, rendered_html)

            status = "BLOCKED" if is_blocked else ("SUCCESS" if http_status < 400 else "ERROR")

            record = {
                "url": current_url,
                "final_url": final_url,
                "status": status,
                "http_status": http_status,
                "title": extracted["title"],
                "meta_description": extracted["meta_description"],
                "word_count": extracted["word_count"],
                "character_count": extracted["character_count"],
                "markdown": extracted["markdown"],
                "plain_text": extracted["plain_text"],
                "rendered_html": rendered_html,
                "error": {"code": "access_challenge", "message": marker} if is_blocked else None,
                "depth": depth,
                "fetch_time_ms": fetch_time_ms
            }

            if status == "SUCCESS":
                self.stats["successful"] += 1
                self.stats["total_words"] += extracted["word_count"]
                self.stats["total_bytes"] += len(rendered_html.encode("utf-8"))
            elif status == "BLOCKED":
                self.stats["blocked"] += 1
            else:
                self.stats["failed"] += 1

            self.results.append(record)

            # Link discovery for deep crawling
            if depth < self.config.max_depth and status == "SUCCESS":
                soup = BeautifulSoup(rendered_html, "html.parser")
                for a in soup.find_all("a", href=True):
                    raw_href = a["href"].strip()
                    norm = normalize_url(raw_href, final_url)
                    if norm and norm not in self.seen_urls:
                        if is_in_scope(norm, self.config.seed_url, self.config.allow_subdomains):
                            safe_l, _ = is_safe_url(norm)
                            if safe_l:
                                self.seen_urls.add(norm)
                                await request_queue.add_request(Request.from_url(norm, user_data={"depth": depth + 1}))

            self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)

            if on_page_crawled:
                try:
                    on_page_crawled(record, self.stats)
                except Exception:
                    pass

        @crawler.failed_request_handler
        async def failed_request_handler(context: PlaywrightCrawlingContext, error: Exception) -> None:
            req = getattr(context, "request", None)
            current_url = getattr(req, "url", "") or "Unknown URL"
            user_data = getattr(req, "user_data", {}) or {}
            depth = user_data.get("depth", 0)

            # Resilient HTTP fallback before giving up
            fb_status, fb_html, final_url = fetch_html_fallback(current_url, self.config.user_agent, timeout=self.config.timeout)
            if fb_html and fb_status < 400:
                t0 = time.perf_counter()
                extracted = extract_exact_content(fb_html, final_url)
                is_blocked, marker = detect_access_challenge(fb_status, fb_html)
                status = "BLOCKED" if is_blocked else "SUCCESS"

                rec = {
                    "url": current_url,
                    "final_url": final_url,
                    "status": status,
                    "http_status": fb_status,
                    "title": extracted["title"],
                    "meta_description": extracted["meta_description"],
                    "word_count": extracted["word_count"],
                    "character_count": extracted["character_count"],
                    "markdown": extracted["markdown"],
                    "plain_text": extracted["plain_text"],
                    "rendered_html": fb_html,
                    "error": {"code": "access_challenge", "message": marker} if is_blocked else None,
                    "depth": depth,
                    "fetch_time_ms": round((time.perf_counter() - t0) * 1000.0, 2)
                }
                self.stats["attempted"] += 1
                if status == "SUCCESS":
                    self.stats["successful"] += 1
                    self.stats["total_words"] += extracted["word_count"]
                    self.stats["total_bytes"] += len(fb_html.encode("utf-8"))
                else:
                    self.stats["blocked"] += 1

                self.results.append(rec)

                # Link discovery for HTTP fallback
                if depth < self.config.max_depth and status == "SUCCESS":
                    soup = BeautifulSoup(fb_html, "html.parser")
                    for a in soup.find_all("a", href=True):
                        raw_href = a["href"].strip()
                        norm = normalize_url(raw_href, final_url)
                        if norm and norm not in self.seen_urls:
                            if is_in_scope(norm, self.config.seed_url, self.config.allow_subdomains):
                                safe_l, _ = is_safe_url(norm)
                                if safe_l:
                                    self.seen_urls.add(norm)
                                    await request_queue.add_request(Request.from_url(norm, user_data={"depth": depth + 1}))

                self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)
                if on_page_crawled:
                    try:
                        on_page_crawled(rec, self.stats)
                    except Exception:
                        pass
                return

            self.stats["attempted"] += 1
            self.stats["failed"] += 1

            resp = getattr(context, "response", None)
            http_status = getattr(resp, "status", None) or (fb_status if fb_status > 0 else None)

            rec = {
                "url": current_url,
                "final_url": None,
                "status": "ERROR",
                "http_status": http_status,
                "title": "Crawl Error",
                "meta_description": "",
                "word_count": 0,
                "character_count": 0,
                "markdown": "",
                "plain_text": "",
                "rendered_html": "",
                "error": {"code": "crawl_error", "message": str(error)},
                "depth": depth,
                "fetch_time_ms": 0.0
            }
            self.results.append(rec)
            self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)
            if on_page_crawled:
                try:
                    on_page_crawled(rec, self.stats)
                except Exception:
                    pass

        try:
            await crawler.run()
        finally:
            total_in_queue = await request_queue.get_total_count()
            handled_in_queue = await request_queue.get_handled_count()
            remaining = max(0, total_in_queue - handled_in_queue)
            await request_queue.drop()

        self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)

        if len(self.results) >= self.config.max_pages and remaining > 0:
            self.stats["crawl_completion_status"] = "PARTIAL_LIMIT_REACHED"
            self.stats["completion_message"] = (
                f"{len(self.results)} pages extracted. {remaining} additional eligible URLs were not crawled "
                f"because the configured limit ({self.config.max_pages}) was reached."
            )
        else:
            self.stats["crawl_completion_status"] = "COMPLETED_ALL_DISCOVERED"
            self.stats["completion_message"] = f"Crawl completed! Extracted {len(self.results)} pages in {self.stats['elapsed_seconds']}s."

        return self.results

    async def _crawl_fast(
        self,
        on_page_crawled: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]],
        start_time: float,
        effective_delay: float,
        discovered_sitemaps: List[str]
    ) -> List[Dict[str, Any]]:
        """Ultra-fast concurrent async crawler with automatic SPA Chromium promotion."""
        queue: asyncio.Queue = asyncio.Queue()
        norm_seed = normalize_url(self.config.seed_url, self.config.seed_url) or self.config.seed_url
        self.seen_urls.add(norm_seed)
        await queue.put((norm_seed, 0))

        for s_url in discovered_sitemaps:
            if s_url not in self.seen_urls and len(self.seen_urls) < self.config.max_pages * 3:
                self.seen_urls.add(s_url)
                await queue.put((s_url, 1))

        lock = asyncio.Lock()

        async def worker():
            while True:
                if len(self.results) >= self.config.max_pages:
                    break
                try:
                    current_url, depth = await asyncio.wait_for(queue.get(), timeout=1.5)
                except asyncio.TimeoutError:
                    break

                if len(self.results) >= self.config.max_pages:
                    queue.task_done()
                    break

                if effective_delay > 0:
                    await asyncio.sleep(effective_delay)

                if self.config.respect_robots and not self.robots.can_fetch(current_url):
                    rec = {
                        "url": current_url,
                        "final_url": None,
                        "status": "SKIPPED_ROBOTS",
                        "http_status": None,
                        "title": "Disallowed by robots.txt",
                        "word_count": 0,
                        "character_count": 0,
                        "markdown": "",
                        "plain_text": "",
                        "rendered_html": "",
                        "error": None,
                        "depth": depth,
                        "fetch_time_ms": 0.0
                    }
                    async with lock:
                        self.results.append(rec)
                        self.stats["attempted"] += 1
                        self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)
                    if on_page_crawled:
                        try:
                            on_page_crawled(rec, self.stats)
                        except Exception:
                            pass
                    queue.task_done()
                    continue

                t0 = time.perf_counter()
                http_status, html_text, final_url = await asyncio.to_thread(
                    fetch_html_fallback, current_url, self.config.user_agent, self.config.timeout
                )

                # Check if SPA / Dynamic JS rendering is needed (thin DOM or JS app roots)
                is_spa = False
                if html_text and http_status < 400:
                    lower_html = html_text.lower()
                    if ("<div id=\"root\"></div>" in lower_html or "<div id=\"app\"></div>" in lower_html or "<div id=\"__next\"></div>" in lower_html or "you need to enable javascript" in lower_html):
                        is_spa = True

                # If HTTP fetch was empty/blocked/0 or requires dynamic JS, render with Playwright
                if not html_text or http_status == 0 or http_status >= 400 or is_spa:
                    p_status, p_html, p_final = await render_single_page_playwright(current_url, self.config.user_agent, self.config.timeout)
                    if p_html and p_status < 400:
                        http_status, html_text, final_url = p_status, p_html, p_final

                fetch_time_ms = round((time.perf_counter() - t0) * 1000.0, 2)

                if html_text and http_status < 400:
                    extracted = extract_exact_content(html_text, final_url)
                    is_blocked, marker = detect_access_challenge(http_status, html_text)
                    status = "BLOCKED" if is_blocked else "SUCCESS"

                    record = {
                        "url": current_url,
                        "final_url": final_url,
                        "status": status,
                        "http_status": http_status,
                        "title": extracted["title"],
                        "meta_description": extracted["meta_description"],
                        "word_count": extracted["word_count"],
                        "character_count": extracted["character_count"],
                        "markdown": extracted["markdown"],
                        "plain_text": extracted["plain_text"],
                        "rendered_html": html_text,
                        "error": {"code": "access_challenge", "message": marker} if is_blocked else None,
                        "depth": depth,
                        "fetch_time_ms": fetch_time_ms
                    }

                    async with lock:
                        self.results.append(record)
                        self.stats["attempted"] += 1
                        if status == "SUCCESS":
                            self.stats["successful"] += 1
                            self.stats["total_words"] += extracted["word_count"]
                            self.stats["total_bytes"] += len(html_text.encode("utf-8"))
                        else:
                            self.stats["blocked"] += 1
                        self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)

                    # Link discovery
                    if depth < self.config.max_depth and status == "SUCCESS":
                        soup = BeautifulSoup(html_text, "html.parser")
                        for a in soup.find_all("a", href=True):
                            raw_href = a["href"].strip()
                            norm = normalize_url(raw_href, final_url)
                            if norm and norm not in self.seen_urls:
                                if is_in_scope(norm, self.config.seed_url, self.config.allow_subdomains):
                                    safe_l, _ = is_safe_url(norm)
                                    if safe_l:
                                        self.seen_urls.add(norm)
                                        await queue.put((norm, depth + 1))
                else:
                    rec = {
                        "url": current_url,
                        "final_url": None,
                        "status": "ERROR",
                        "http_status": http_status or None,
                        "title": "Crawl Error",
                        "meta_description": "",
                        "word_count": 0,
                        "character_count": 0,
                        "markdown": "",
                        "plain_text": "",
                        "rendered_html": "",
                        "error": {"code": "fetch_error", "message": f"HTTP status {http_status}"},
                        "depth": depth,
                        "fetch_time_ms": fetch_time_ms
                    }
                    async with lock:
                        self.results.append(rec)
                        self.stats["attempted"] += 1
                        self.stats["failed"] += 1
                        self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)
                    record = rec

                if on_page_crawled:
                    try:
                        on_page_crawled(record, self.stats)
                    except Exception:
                        pass
                queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(max(1, self.config.concurrency))]
        await asyncio.gather(*workers)

        self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)
        remaining = queue.qsize()
        if len(self.results) >= self.config.max_pages and remaining > 0:
            self.stats["crawl_completion_status"] = "PARTIAL_LIMIT_REACHED"
            self.stats["completion_message"] = f"{len(self.results)} pages extracted. Reached configured limit ({self.config.max_pages})."
        else:
            self.stats["crawl_completion_status"] = "COMPLETED_ALL_DISCOVERED"
            self.stats["completion_message"] = f"Crawl completed! Extracted {len(self.results)} pages in {self.stats['elapsed_seconds']}s."

        return self.results

    async def _crawl_jina(
        self,
        on_page_crawled: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]],
        start_time: float,
        effective_delay: float,
        discovered_sitemaps: List[str]
    ) -> List[Dict[str, Any]]:
        """Multi-page crawler powered by Jina Reader API (r.jina.ai)."""
        queue: asyncio.Queue = asyncio.Queue()
        norm_seed = normalize_url(self.config.seed_url, self.config.seed_url) or self.config.seed_url
        self.seen_urls.add(norm_seed)
        await queue.put((norm_seed, 0))

        for s_url in discovered_sitemaps:
            if s_url not in self.seen_urls and len(self.seen_urls) < self.config.max_pages * 3:
                self.seen_urls.add(s_url)
                await queue.put((s_url, 1))

        lock = asyncio.Lock()

        async def worker():
            while True:
                if len(self.results) >= self.config.max_pages:
                    break
                try:
                    current_url, depth = await asyncio.wait_for(queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    break

                if len(self.results) >= self.config.max_pages:
                    queue.task_done()
                    break

                if effective_delay > 0:
                    await asyncio.sleep(effective_delay)

                if self.config.respect_robots and not self.robots.can_fetch(current_url):
                    rec = {
                        "url": current_url,
                        "final_url": None,
                        "status": "SKIPPED_ROBOTS",
                        "http_status": None,
                        "title": "Disallowed by robots.txt",
                        "word_count": 0,
                        "character_count": 0,
                        "markdown": "",
                        "plain_text": "",
                        "rendered_html": "",
                        "error": None,
                        "depth": depth,
                        "fetch_time_ms": 0.0
                    }
                    async with lock:
                        self.results.append(rec)
                        self.stats["attempted"] += 1
                        self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)
                    if on_page_crawled:
                        try:
                            on_page_crawled(rec, self.stats)
                        except Exception:
                            pass
                    queue.task_done()
                    continue

                t0 = time.perf_counter()
                jina_res = await asyncio.to_thread(fetch_jina_reader, current_url, self.config.jina_api_key, self.config.timeout)
                fetch_time_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                jina_res["fetch_time_ms"] = fetch_time_ms
                jina_res["depth"] = depth
                jina_res["url"] = current_url

                status = jina_res["status"]
                async with lock:
                    self.results.append(jina_res)
                    self.stats["attempted"] += 1
                    if status == "SUCCESS":
                        self.stats["successful"] += 1
                        self.stats["total_words"] += jina_res["word_count"]
                        self.stats["total_bytes"] += len(jina_res["markdown"].encode("utf-8"))
                    else:
                        self.stats["failed"] += 1
                    self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)

                # Link discovery for deep multi-page crawl
                if depth < self.config.max_depth and status == "SUCCESS":
                    for cand_link in jina_res.get("links", []):
                        if cand_link not in self.seen_urls:
                            if is_in_scope(cand_link, self.config.seed_url, self.config.allow_subdomains):
                                safe_l, _ = is_safe_url(cand_link)
                                if safe_l:
                                    self.seen_urls.add(cand_link)
                                    await queue.put((cand_link, depth + 1))

                if on_page_crawled:
                    try:
                        on_page_crawled(jina_res, self.stats)
                    except Exception:
                        pass
                queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(max(1, self.config.concurrency))]
        await asyncio.gather(*workers)

        self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)
        remaining = queue.qsize()
        if len(self.results) >= self.config.max_pages and remaining > 0:
            self.stats["crawl_completion_status"] = "PARTIAL_LIMIT_REACHED"
            self.stats["completion_message"] = f"{len(self.results)} pages extracted via Jina Reader. Reached configured limit ({self.config.max_pages})."
        else:
            self.stats["crawl_completion_status"] = "COMPLETED_ALL_DISCOVERED"
            self.stats["completion_message"] = f"Crawl completed! Extracted {len(self.results)} pages via Jina Reader in {self.stats['elapsed_seconds']}s."

        return self.results


# -----------------------------------------------------------------------------
# CLI RUNNER
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Production Web Crawler & Structured Engine")
    parser.add_argument("url", nargs="?", default="https://www.nvidia.com/en-in/", help="Seed URL")
    parser.add_argument("--max-pages", type=int, default=5, help="Max pages to crawl")
    parser.add_argument("--max-depth", type=int, default=1, help="Max depth")
    parser.add_argument("--concurrency", type=int, default=2, help="Browser concurrency")
    parser.add_argument("--delay", type=float, default=0.3, help="Politeness delay (seconds)")
    parser.add_argument("--output", type=str, default="output.jsonl", help="Output file path (.jsonl)")

    args = parser.parse_args()

    print(f"\n[CLI Crawl] Target: {args.url} (max_pages={args.max_pages}, depth={args.max_depth})\n" + "=" * 75)

    cfg = CrawlConfig(
        seed_url=args.url,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        concurrency=args.concurrency,
        delay=args.delay
    )
    c = CrawleeWebCrawler(cfg)
    res = asyncio.run(c.crawl())

    print(f"Status      : {c.stats['crawl_completion_status']}")
    print(f"Total Words : {c.stats['total_words']:,} across {len(res)} pages")
    print(f"Elapsed     : {c.stats['elapsed_seconds']}s")
    print("=" * 75)

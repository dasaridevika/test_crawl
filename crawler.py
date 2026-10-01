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
    "Chrome/124.0.0.0 Safari/537.36 (Compatible; Crawlee-Playwright/1.0)"
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

    # Decompose non-content, media, and form tags (No images, No SVGs)
    for tag in content_soup(["script", "style", "noscript", "svg", "img", "picture", "source", "canvas", "video", "audio", "iframe", "button", "input", "select", "option", "form"]):
        tag.decompose()

    # Remove HTML comments
    for comment in content_soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Remove Cookie Banners & Consent Modals
    cookie_and_modal_selectors = [
        "#onetrust-consent-sdk", "#onetrust-banner-sdk", "#onetrust-pc-sdk",
        ".optanon-alert-box-wrapper", ".cookie-banner", ".ot-sdk-container",
        ".region-selector-modal", ".country-selector", ".modal-backdrop",
        "[id*='cookie']", "[id*='consent']", "[class*='cookie']", "[class*='consent']",
        "[role='dialog']", "[role='alertdialog']", "[aria-modal='true']",
        ".skip-to-content", ".skip-link"
    ]
    for sel in cookie_and_modal_selectors:
        for el in content_soup.select(sel):
            el.decompose()

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
    user_agent: str = USER_AGENT


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
        logger.info(f"Starting Crawl on {self.config.seed_url} (max_pages={self.config.max_pages}, max_depth={self.config.max_depth})")

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

        # 4. Initialize Unique RequestQueue
        queue_name = f"crawl_queue_{int(time.time() * 1000)}"
        request_queue = await RequestQueue.open(name=queue_name)
        norm_seed = normalize_url(self.config.seed_url, self.config.seed_url) or self.config.seed_url
        self.seen_urls.add(norm_seed)
        await request_queue.add_request(Request.from_url(norm_seed, user_data={"depth": 0}))

        for s_url in discovered_sitemaps:
            if s_url not in self.seen_urls and len(self.seen_urls) < self.config.max_pages * 3:
                self.seen_urls.add(s_url)
                await request_queue.add_request(Request.from_url(s_url, user_data={"depth": 1}))

        # 5. Initialize Crawlee PlaywrightCrawler with Linux sandbox flags
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
                    "--disable-gpu",
                    "--single-process" if sys.platform != "win32" and "STREAMLIT" in os.environ else ""
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
                    await page.wait_for_load_state("domcontentloaded")
                    await page.evaluate("""async () => {
                        window.scrollTo(0, document.body.scrollHeight / 2);
                        await new Promise(r => setTimeout(r, 200));
                        window.scrollTo(0, document.body.scrollHeight);
                        await new Promise(r => setTimeout(r, 200));
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
            self.stats["attempted"] += 1
            self.stats["failed"] += 1

            resp = getattr(context, "response", None)
            http_status = getattr(resp, "status", None)

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

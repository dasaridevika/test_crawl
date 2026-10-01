"""
Layer 1: Single-Page Browser Fetcher & Renderer
================================================
Fetches and renders a single URL using Crawlee + Playwright (Headless Chromium).
Executes client-side JavaScript, handles dynamic hydration, and triggers lazy-loaded elements.
"""

import asyncio
import ipaddress
import logging
import re
import socket
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("Layer1Fetcher")

# -----------------------------------------------------------------------------
# SSRF & URL SAFETY VALIDATOR
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
    Validates URL scheme and resolves DNS to ensure the request is not directed
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
    """Checks if the response indicates an access challenge / bot block."""
    if status_code in (401, 403):
        return True, f"http_{status_code}_access_denied"
    if status_code == 429:
        return True, "http_429_rate_limited"

    for pattern in ACCESS_CHALLENGE_PATTERNS:
        if pattern.search(html_content):
            return True, "access_challenge_detected"
    return False, None


# -----------------------------------------------------------------------------
# SINGLE-PAGE FETCHER CORE
# -----------------------------------------------------------------------------
async def fetch_page(url: str, timeout: int = 30) -> Dict[str, Any]:
    """
    Fetches and renders a single page using Crawlee PlaywrightCrawler.
    Executes client-side JavaScript, auto-scrolls to trigger lazy loading,
    and captures the rendered HTML.
    """
    safe, reason = is_safe_url(url)
    if not safe:
        return {
            "url": url,
            "final_url": None,
            "status": "ERROR_SECURITY",
            "http_status": None,
            "title": "Security Error",
            "rendered_html": "",
            "fetch_time_ms": 0.0,
            "content_bytes": 0,
            "error": {"code": "security_block", "message": f"Target URL rejected by security policy: {reason}"}
        }

    result: Dict[str, Any] = {
        "url": url,
        "final_url": url,
        "status": "ERROR",
        "http_status": None,
        "title": "",
        "rendered_html": "",
        "fetch_time_ms": 0.0,
        "content_bytes": 0,
        "error": None
    }

    t0 = time.perf_counter()

    crawler = PlaywrightCrawler(
        max_requests_per_crawl=1,
        max_request_retries=1,
        headless=True,
        browser_new_context_options={"user_agent": USER_AGENT}
    )

    @crawler.router.default_handler
    async def request_handler(context: PlaywrightCrawlingContext) -> None:
        page = context.page
        current_url = context.request.url

        # Wait for initial DOM
        try:
            await page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass

        # Smooth auto-scroll to trigger lazy-loaded images & card components
        try:
            await page.evaluate("""async () => {
                window.scrollTo(0, document.body.scrollHeight / 2);
                await new Promise(r => setTimeout(r, 250));
                window.scrollTo(0, document.body.scrollHeight);
                await new Promise(r => setTimeout(r, 250));
                window.scrollTo(0, 0);
            }""")
        except Exception:
            pass

        rendered_html = await page.content()
        final_url = page.url or current_url
        http_status = context.response.status if context.response else 200

        # Extract title
        soup = BeautifulSoup(rendered_html, "html.parser")
        title_el = soup.find("title") or soup.find("h1")
        title = title_el.get_text(" ", strip=True) if title_el else "Untitled Page"

        # Check for challenge or block
        is_blocked, marker = detect_access_challenge(http_status, rendered_html)
        status = "BLOCKED" if is_blocked else ("SUCCESS" if http_status < 400 else "ERROR")

        result["url"] = current_url
        result["final_url"] = final_url
        result["status"] = status
        result["http_status"] = http_status
        result["title"] = title
        result["rendered_html"] = rendered_html
        result["fetch_time_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        result["content_bytes"] = len(rendered_html.encode("utf-8"))

        if is_blocked:
            result["error"] = {"code": "access_challenge", "message": f"Access challenge detected: {marker}"}
        elif http_status >= 400:
            result["error"] = {"code": "http_error", "message": f"HTTP error {http_status}"}

    @crawler.failed_request_handler
    async def failed_request_handler(context: PlaywrightCrawlingContext, error: Exception) -> None:
        result["status"] = "ERROR"
        result["http_status"] = getattr(getattr(context, "response", None), "status", None)
        result["fetch_time_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        result["error"] = {"code": "fetch_failure", "message": str(error)}

    await crawler.run([Request.from_url(url)])
    return result


# -----------------------------------------------------------------------------
# CLI TEST ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    # Set stdout to utf-8 if supported
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.nvidia.com/en-in/"
    print(f"\n[Layer 1 Test] Fetching and rendering: {test_url}\n" + "-" * 60)

    res = asyncio.run(fetch_page(test_url))

    print(f"Status       : {res['status']} (HTTP {res['http_status']})")
    print(f"Title        : {res['title']}")
    print(f"Final URL    : {res['final_url']}")
    print(f"Latency      : {res['fetch_time_ms']} ms")
    print(f"DOM Size     : {res['content_bytes']:,} bytes")
    if res['error']:
        print(f"Error Details: {res['error']}")
    print("-" * 60)
    print(f"HTML Preview (first 300 chars):\n{res['rendered_html'][:300]}...\n")

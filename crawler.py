"""
Layer 4: Production-Grade Multi-Page Web Crawler & Structured Engine
===================================================================
Built on Crawlee for Python + PlaywrightCrawler (Headless Chromium).

Incorporates:
- Layer 1: JavaScript hydration, auto-scroll lazy-loading, and SSRF security protection.
- Layer 2: 100% lossless exact content extraction (zero truncation, full text, markdown, rendered HTML).
- Layer 3: URL Frontier (RequestQueue deduplication), domain scope matching, robots.txt caching,
  and sitemap discovery.
- Layer 4: Multi-worker concurrency, depth tracking, link discovery, and progress streaming.
"""

import argparse
import asyncio
import html as html_lib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment
from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.storages import RequestQueue
import markdownify

from layer1_fetcher import detect_access_challenge, is_safe_url
from layer2_extractor import extract_exact_content
from layer3_frontier import RobotsManager, SitemapDiscoverer, is_in_scope, normalize_url

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("CrawleeCrawlerEngine")


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
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 (Compatible; Crawlee-Playwright/1.0)"
    )


# -----------------------------------------------------------------------------
# LINK EXTRACTOR (Frontier Expansion)
# -----------------------------------------------------------------------------
def extract_outbound_links(rendered_soup: BeautifulSoup, current_url: str, seed_url: str, allow_subdomains: bool) -> List[str]:
    """Extracts in-scope links for queueing into the frontier."""
    links: List[str] = []
    seen: Set[str] = set()

    for a in rendered_soup.find_all("a", href=True):
        raw_href = a["href"].strip()
        norm = normalize_url(raw_href, current_url)
        if norm and norm not in seen:
            if is_in_scope(norm, seed_url, allow_subdomains):
                safe, _ = is_safe_url(norm)
                if safe:
                    seen.add(norm)
                    links.append(norm)

    return links


# -----------------------------------------------------------------------------
# CRAWLEE PLAYWRIGHT CRAWLER ENGINE
# -----------------------------------------------------------------------------
class CrawleeWebCrawler:
    """Multi-page crawler engine built on Crawlee PlaywrightCrawler."""

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

        # 1. Security check on seed URL
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

        # 2. Robots.txt compliance & Crawl-delay calculation
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
            logger.info(f"Discovered {len(discovered_sitemaps)} seed URLs from sitemaps")

        # 4. Initialize Crawlee RequestQueue
        request_queue = await RequestQueue.open()
        norm_seed = normalize_url(self.config.seed_url, self.config.seed_url) or self.config.seed_url
        self.seen_urls.add(norm_seed)
        await request_queue.add_request(Request.from_url(norm_seed, user_data={"depth": 0}))

        for s_url in discovered_sitemaps:
            if s_url not in self.seen_urls and len(self.seen_urls) < self.config.max_pages * 3:
                self.seen_urls.add(s_url)
                await request_queue.add_request(Request.from_url(s_url, user_data={"depth": 1}))

        # 5. Initialize Crawlee PlaywrightCrawler
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
            browser_new_context_options={"user_agent": self.config.user_agent}
        )

        @crawler.router.default_handler
        async def request_handler(context: PlaywrightCrawlingContext) -> None:
            current_url = getattr(getattr(context, "request", None), "url", "")
            user_data = getattr(getattr(context, "request", None), "user_data", {}) or {}
            depth = user_data.get("depth", 0)
            self.stats["attempted"] += 1

            # Politeness pacing
            if effective_delay > 0:
                await asyncio.sleep(effective_delay)

            # Robots.txt permission check
            if self.config.respect_robots and not self.robots.can_fetch(current_url):
                logger.info(f"Robots.txt skipped: {current_url}")
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

            # Extract 100% complete exact content via Layer 2
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

            # Update stats
            if status == "SUCCESS":
                self.stats["successful"] += 1
                self.stats["total_words"] += extracted["word_count"]
                self.stats["total_bytes"] += len(rendered_html.encode("utf-8"))
            elif status == "BLOCKED":
                self.stats["blocked"] += 1
            else:
                self.stats["failed"] += 1

            self.results.append(record)

            # Deep Link Discovery & Queueing (up to max_depth)
            if depth < self.config.max_depth and status == "SUCCESS":
                soup = BeautifulSoup(rendered_html, "html.parser")
                out_links = extract_outbound_links(soup, final_url, self.config.seed_url, self.config.allow_subdomains)
                for out_url in out_links:
                    if out_url not in self.seen_urls:
                        self.seen_urls.add(out_url)
                        await request_queue.add_request(Request.from_url(out_url, user_data={"depth": depth + 1}))

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

        # Execute the crawl
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
    import json
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Layer 4 Multi-Page Web Crawler & Structured Engine")
    parser.add_argument("url", nargs="?", default="https://www.nvidia.com/en-in/", help="Seed URL")
    parser.add_argument("--max-pages", type=int, default=5, help="Max pages to crawl")
    parser.add_argument("--max-depth", type=int, default=1, help="Max depth")
    parser.add_argument("--concurrency", type=int, default=2, help="Browser concurrency")
    parser.add_argument("--delay", type=float, default=0.3, help="Politeness delay (seconds)")
    parser.add_argument("--output", type=str, default="output.jsonl", help="Output file path (.jsonl)")

    args = parser.parse_args()

    print(f"\n[Layer 4 Test] Running Multi-Page Crawl on {args.url} (max_pages={args.max_pages}, depth={args.max_depth}, concurrency={args.concurrency})\n" + "=" * 75)

    config = CrawlConfig(
        seed_url=args.url,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        concurrency=args.concurrency,
        delay=args.delay
    )

    crawler = CrawleeWebCrawler(config)

    def progress_cb(rec, stats):
        print(f"[{stats['attempted']}/{config.max_pages}] {rec['status']} (HTTP {rec['http_status']}) | {rec['word_count']:,} words | {rec['title'][:40]} | {rec['url']}")

    results = asyncio.run(crawler.crawl(on_page_crawled=progress_cb))

    print("\n" + "=" * 75)
    print(f"Crawl Status : {crawler.stats['crawl_completion_status']}")
    print(f"Summary      : {crawler.stats['completion_message']}")
    print(f"Total Words  : {crawler.stats['total_words']:,} words across {len(results)} pages")
    print(f"Elapsed Time : {crawler.stats['elapsed_seconds']}s")
    print("=" * 75)

    # Save to JSONL
    with open(args.output, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Saved dataset to: {args.output}\n")

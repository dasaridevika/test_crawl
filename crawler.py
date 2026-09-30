"""
Production-Grade Anti-Bot Resistant Web Crawler
================================================
Combines:
1. Crawlee PlaywrightCrawler for JS-rendered & protected challenge pages.
2. High-speed curl_cffi for direct TLS-impersonated HTTP extraction.
3. Automated HTML cleanup and Clean Markdown/JSON generation.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.models import Request
from curl_cffi.requests import AsyncSession
import defusedxml.ElementTree as ET
from markdownify import markdownify as md

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("ProductionCrawler")


def clean_html_to_markdown(html_content: str) -> str:
    """
    Cleans boilerplate elements (nav, footer, ads, cookie banners, scripts)
    and converts the core HTML content into structured Markdown.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Strip non-content and noise tags
    noise_selectors = [
        "script", "style", "noscript", "svg", "iframe",
        "nav", "footer", "header", "aside",
        ".cookie-banner", "#cookie-notice", ".advertisement", ".ad-container",
        "[role='banner']", "[role='navigation']"
    ]
    for selector in noise_selectors:
        for element in soup.select(selector):
            element.decompose()

    # Prioritize main article/content container if present
    main_content = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", {"id": "content"})
        or soup.find("body")
        or soup
    )

    raw_markdown = md(str(main_content), heading_style="ATX", strip=["img", "a"])
    # Clean up redundant newlines
    lines = [line.strip() for line in raw_markdown.splitlines()]
    clean_markdown = "\n".join(line for line in lines if line)
    return clean_markdown


async def discover_sitemap_urls(session: AsyncSession, base_url: str) -> list[str]:
    """
    Attempts to discover target URLs directly from standard sitemap locations.
    """
    parsed = urlparse(base_url)
    sitemap_candidates = [
        f"{parsed.scheme}://{parsed.netloc}/sitemap.xml",
        f"{parsed.scheme}://{parsed.netloc}/sitemap_index.xml",
    ]

    for sitemap_url in sitemap_candidates:
        try:
            logger.info(f"Checking sitemap at: {sitemap_url}")
            resp = await session.get(sitemap_url, impersonate="chrome120", timeout=8)
            if resp.status_code == 200 and b"<urlset" in resp.content or b"<sitemapindex" in resp.content:
                root = ET.fromstring(resp.content)
                namespaces = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                urls = [elem.text for elem in root.findall(".//ns:loc", namespaces) if elem.text]
                if urls:
                    logger.info(f"Discovered {len(urls)} URLs directly from sitemap!")
                    return urls
        except Exception as e:
            logger.debug(f"Sitemap lookup failed for {sitemap_url}: {e}")

    return []


async def run_fast_tls_crawler(
    urls: list[str],
    output_file: Path,
    max_concurrency: int = 8,
    delay: float = 0.5
) -> None:
    """
    Fast Direct HTTP crawler with TLS/JA3 Chrome impersonation (curl_cffi).
    """
    logger.info(f"Starting Fast TLS Impersonation crawl on {len(urls)} URLs...")
    results = []
    semaphore = asyncio.Semaphore(max_concurrency)

    async with AsyncSession() as session:
        async def fetch(url: str):
            async with semaphore:
                await asyncio.sleep(delay)
                try:
                    resp = await session.get(url, impersonate="chrome120", timeout=12)
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"
                        markdown = clean_html_to_markdown(resp.text)
                        
                        record = {
                            "url": url,
                            "title": title,
                            "markdown": markdown,
                            "status": "SUCCESS"
                        }
                        results.append(record)
                        logger.info(f"[200 OK] {url} (Title: {title[:40]}...)")
                    else:
                        logger.warning(f"[{resp.status_code}] Failed to fetch {url}")
                except Exception as e:
                    logger.error(f"Error fetching {url}: {e}")

        tasks = [fetch(url) for url in urls]
        await asyncio.gather(*tasks)

    # Save output
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    logger.info(f"✓ Saved {len(results)} records to {output_file}")


async def run_playwright_stealth_crawler(
    seed_urls: list[str],
    output_file: Path,
    max_requests: int = 50,
    max_concurrency: int = 4
) -> None:
    """
    Production stealth crawler with full JavaScript evaluation (Crawlee Playwright).
    """
    logger.info(f"Starting Playwright Stealth crawler with max {max_requests} pages...")
    results = []

    crawler = PlaywrightCrawler(
        max_requests_per_crawl=max_requests,
        max_concurrency=max_concurrency,
        headless=True,
        browser_type="chromium",
    )

    @crawler.router.default_handler
    async def request_handler(context: PlaywrightCrawlingContext) -> None:
        url = context.request.url
        logger.info(f"Crawling (Playwright): {url}")

        # Wait for dynamic DOM completion
        await context.page.wait_for_load_state("domcontentloaded")
        
        # Extract title and HTML
        title = await context.page.title()
        html = await context.page.content()
        markdown = clean_html_to_markdown(html)

        record = {
            "url": url,
            "title": title,
            "markdown": markdown,
            "status": "SUCCESS"
        }
        results.append(record)

        # Enqueue internal domain links automatically (Breadth-First Traversal)
        await context.enqueue_links(strategy="same-domain")

    initial_requests = [Request.from_url(url) for url in seed_urls]
    await crawler.run(initial_requests)

    # Save output
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    logger.info(f"✓ Saved {len(results)} records to {output_file}")


async def main():
    parser = argparse.ArgumentParser(description="Production-Grade Anti-Bot Resistant Web Crawler")
    parser.add_argument("url", help="Target seed URL (e.g., https://quotes.toscrape.com/js/)")
    parser.add_argument(
        "--mode",
        choices=["fast", "stealth", "auto"],
        default="auto",
        help="Crawl mode: 'fast' (TLS HTTP via curl_cffi), 'stealth' (Playwright JS engine), or 'auto' (Sitemap discovery + fast fallback)."
    )
    parser.add_argument("--max-pages", type=int, default=30, help="Maximum number of pages to crawl")
    parser.add_argument("--concurrency", type=int, default=5, help="Number of concurrent workers")
    parser.add_argument("--output", default="output.json", help="Output JSON file path")

    args = parser.parse_args()
    output_path = Path(args.output).resolve()

    if args.mode == "auto":
        async with AsyncSession() as session:
            sitemap_urls = await discover_sitemap_urls(session, args.url)
            
        if sitemap_urls:
            logger.info(f"Sitemap detected. Using fast TLS extraction for top {args.max_pages} URLs.")
            await run_fast_tls_crawler(
                sitemap_urls[:args.max_pages],
                output_path,
                max_concurrency=args.concurrency
            )
        else:
            logger.info("No sitemap found. Falling back to Playwright Stealth crawler with link discovery.")
            await run_playwright_stealth_crawler(
                [args.url],
                output_path,
                max_requests=args.max_pages,
                max_concurrency=args.concurrency
            )
    elif args.mode == "fast":
        await run_fast_tls_crawler([args.url], output_path, max_concurrency=args.concurrency)
    elif args.mode == "stealth":
        await run_playwright_stealth_crawler(
            [args.url],
            output_path,
            max_requests=args.max_pages,
            max_concurrency=args.concurrency
        )


if __name__ == "__main__":
    asyncio.run(main())

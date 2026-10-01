"""
Layer 2: Pure Text & Markdown Content Extractor (No Image Clutter, No Cookie Banners)
====================================================================================
Extracts 100% complete, un-truncated editorial content:
- Removes cookie consent banners (OneTrust, Optanon, Cookiebot, etc.)
- Strips all images, icons, and SVG data URIs for clean, distraction-free reading
- Preserves exact headings (#, ##, ###), paragraphs, lists (-), and data tables
- Resolves real hyperlinks while eliminating broken '#' fragment anchors
"""

import html as html_lib
import re
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, Tag
import markdownify


def clean_whitespace(text: str) -> str:
    """Normalizes excessive blank lines while preserving paragraph and heading structure."""
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

    # 1. Page Title & Meta
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

    # 2. Build Clean Content DOM
    content_soup = BeautifulSoup(rendered_html, "html.parser")

    # Remove non-content & media tags completely (No images, No SVGs, No video/audio)
    for tag in content_soup(["script", "style", "noscript", "svg", "img", "picture", "source", "canvas", "video", "audio", "iframe", "button", "input", "select", "option", "form"]):
        tag.decompose()

    # Remove HTML comments
    for comment in content_soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Remove Cookie Banners, Consent Modals, and Overlay Clutter
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

    # Clean links: Keep real http/https links, remove empty '#' or javascript links
    for a in body.find_all("a"):
        href = a.get("href", "").strip()
        link_text = a.get_text(" ", strip=True)
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")) or not link_text:
            a.unwrap()
        else:
            abs_url = urljoin(url, href)
            a["href"] = abs_url

    # Convert to clean, readable Markdown (headings, lists, paragraphs, tables)
    cleaned_html = str(body)
    
    markdown_content = markdownify.markdownify(
        cleaned_html,
        heading_style="ATX",
        bullets_style="-",
        autolinks=False,
        strip=["img", "picture", "svg", "canvas", "script", "style"]
    )
    markdown_content = clean_whitespace(markdown_content)

    # Convert to pure plain text
    plain_text = body.get_text(separator="\n", strip=True)
    plain_text = clean_whitespace(plain_text)

    # Calculate word count & character count
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
# CLI TEST ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    import sys
    from layer1_fetcher import fetch_page

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.nvidia.com/en-gb/about-nvidia"
    print(f"\n[Testing Clean Content Extractor on]: {test_url}\n" + "=" * 75)

    fetch_res = asyncio.run(fetch_page(test_url))
    if fetch_res["status"] != "SUCCESS":
        print(f"Fetch failed: {fetch_res['status']}")
        sys.exit(1)

    extracted = extract_exact_content(fetch_res["rendered_html"], fetch_res["final_url"])

    print(f"Title       : {extracted['title']}")
    print(f"Total Words : {extracted['word_count']:,} words")
    print(f"Total Chars : {extracted['character_count']:,} chars")
    print("=" * 75)
    print("\n--- CLEAN MARKDOWN CONTENT PREVIEW ---\n")
    print(extracted["markdown"][:2500])
    print("\n...\n" + "=" * 75)

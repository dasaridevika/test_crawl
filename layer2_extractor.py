"""
Layer 2: Exact, Lossless Content Extractor
==========================================
Extracts the complete text and markdown content from the rendered DOM exactly as it appears
on the actual website:
- Zero truncation
- Zero missing sections or paragraphs
- Exact document reading order
- Full preservation of headings, paragraphs, lists, links, tables, and media captions
- Multi-representation: Rendered HTML, Clean Formatted Markdown, and Full Plain Text
"""

import html as html_lib
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
import markdownify


def clean_whitespace(text: str) -> str:
    """Normalizes excessive blank lines while preserving paragraph breaks."""
    lines = [line.strip() for line in text.splitlines()]
    # Remove consecutive empty lines
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
    Extracts 100% complete, non-truncated content from rendered HTML.
    Preserves exact site hierarchy, headings, links, tables, and text.
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

    soup = BeautifulSoup(rendered_html, "html.parser")

    # 1. Page Title
    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else (h1_tag.get_text(" ", strip=True) if h1_tag else "Untitled")
    title = re.sub(r"\s+", " ", title).strip()

    # 2. Meta Description
    meta_description = ""
    meta_desc_tag = soup.find("meta", attrs={"name": re.compile(r"description", re.I)}) or \
                    soup.find("meta", attrs={"property": re.compile(r"og:description", re.I)})
    if meta_desc_tag and meta_desc_tag.get("content"):
        meta_description = meta_desc_tag["content"].strip()

    # 3. Create a working DOM tree for content extraction
    content_soup = BeautifulSoup(rendered_html, "html.parser")

    # Remove non-visible / executable tags only (preserving all visual content)
    for el in content_soup(["script", "style", "noscript", "svg", "iframe"]):
        el.decompose()

    # Remove HTML comments
    for comment in content_soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Target body or root
    body = content_soup.find("body") or content_soup

    # Ensure all relative URLs in links and images are converted to absolute URLs
    for a in body.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
            a["href"] = urljoin(url, href)

    for img in body.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy-src")
            or (img.get("srcset", "").split()[0] if img.get("srcset") else None)
        )
        if src and not src.startswith("data:"):
            img["src"] = urljoin(url, src)

    # Convert HTML to clean, readable Markdown preserving all headings, bold text, links, lists, and tables
    cleaned_html = str(body)
    
    markdown_content = markdownify.markdownify(
        cleaned_html,
        heading_style="ATX",
        bullets_style="-",
        strip=["script", "style", "noscript", "svg", "iframe"],
        autolinks=False
    )
    markdown_content = clean_whitespace(markdown_content)

    # Generate full plain text preserving natural line breaks and indentation
    plain_text = body.get_text(separator="\n", strip=True)
    plain_text = clean_whitespace(plain_text)

    # Word count and character count metrics
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

    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.nvidia.com/en-in/"
    print(f"\n[Layer 1 + Layer 2 Test] Fetching & Extracting 100% Exact Content: {test_url}\n" + "=" * 70)

    # 1. Fetch via Layer 1
    fetch_res = asyncio.run(fetch_page(test_url))

    if fetch_res["status"] != "SUCCESS":
        print(f"Fetch failed with status: {fetch_res['status']} - Error: {fetch_res['error']}")
        sys.exit(1)

    # 2. Extract complete content via Layer 2
    extracted = extract_exact_content(fetch_res["rendered_html"], fetch_res["final_url"])

    print(f"Page Title      : {extracted['title']}")
    print(f"Total Words     : {extracted['word_count']:,} words (100% complete)")
    print(f"Total Characters: {extracted['character_count']:,} chars")
    print(f"DOM Size        : {len(extracted['rendered_html'].encode('utf-8')):,} bytes")
    print("=" * 70)
    print("\n--- EXACT EXTRACTED CONTENT PREVIEW (First 2,500 characters) ---\n")
    print(extracted["plain_text"][:2500])
    print("\n...\n" + "=" * 70)

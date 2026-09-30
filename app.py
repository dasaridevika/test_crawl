import asyncio
import heapq
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse

import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
import markdownify

# -----------------------------------------------------------------------------
# SAFE IMPORTS & ENVIRONMENT SETUP
# -----------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def setup_playwright_environment():
    """Ensures Chromium binary is downloaded for Playwright/Crawl4AI on Streamlit Cloud."""
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    CRAWL4AI_AVAILABLE = True
    setup_playwright_environment()
except ImportError:
    CRAWL4AI_AVAILABLE = False

try:
    from curl_cffi.requests import AsyncSession as CffiAsyncSession
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False


# -----------------------------------------------------------------------------
# PAGE CONFIGURATION & STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Adaptive Frontier Web Extractor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
    .main-header { font-size: 2.2rem; font-weight: 700; margin-bottom: 0.2rem; }
    .sub-header { color: #888; font-size: 0.95rem; margin-bottom: 1.2rem; }
    .stMarkdown { font-size: 1rem; line-height: 1.7; }
    </style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# PRIORITY FRONTIER DATA STRUCTURES
# -----------------------------------------------------------------------------
class FrontierItem:
    """Priority queue item ordered by score (highest score first)."""
    def __init__(self, priority: float, url: str, depth: int):
        self.priority = priority
        self.url = url
        self.depth = depth

    def __lt__(self, other):
        # Max-heap behavior
        return self.priority > other.priority


def calculate_url_priority(url: str, depth: int) -> float:
    """Heuristic scoring: prioritizes content paths and penalizes depth."""
    score = 100.0 - (depth * 25.0)
    valuable_keywords = ["/product/", "/article/", "/doc/", "/data/", "/item/", "/blog/", "/news/"]
    if any(k in url.lower() for k in valuable_keywords):
        score += 35.0
    utility_keywords = ["/tag/", "/page/", "/category/", "/search/", "/login", "/terms"]
    if any(k in url.lower() for k in utility_keywords):
        score -= 20.0
    return score


def normalize_target_url(raw_url: str, base_domain: str, current_url: str) -> str | None:
    """Normalizes URLs and enforces strict same-domain boundaries."""
    if not raw_url or raw_url.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    full_url = urljoin(current_url, raw_url).split("#")[0].rstrip("/")
    parsed = urlparse(full_url)
    if parsed.netloc == base_domain and parsed.scheme in ("http", "https"):
        return full_url
    return None


def extract_exact_structured_markdown(soup_root: BeautifulSoup, base_url: str) -> str:
    """Compiles complete, clean, hierarchical Markdown containing all editorial copy and paragraphs."""
    s = BeautifulSoup(str(soup_root), "html.parser")

    # 1. Decompose noisy non-content structural tags
    for el in s.find_all(["script", "style", "noscript", "svg", "iframe", "button", "form", "nav", "header", "footer"]):
        el.decompose()

    # 2. Decompose UI modals, country selectors, and carousel indicator dots
    noise_matchers = [
        "cmp-carousel__indicators", "cmp-carousel__actions", "carousel-indicators", "carousel-control", 
        "slider-nav", "slider-pagination", "slick-dots", "cookie", "modal", "drawer", 
        "country-selector", "location-selector", "sr-only", "region-selector"
    ]
    for el in s.find_all(lambda e: e.name not in ["html", "body"] and any(m in str(e.get("class", "")).lower() or m in str(e.get("id", "")).lower() for m in noise_matchers)):
        el.decompose()

    # 3. Convert all relative URLs to absolute URLs
    for a in s.find_all("a", href=True):
        a["href"] = urljoin(base_url, a["href"])

    # 4. Extract full content with markdownify
    body = s.find("body") or s
    md = markdownify.markdownify(
        str(body),
        heading_style="ATX",
        bullets="-",
        strip=["script", "style", "button", "form", "nav", "svg", "img", "noscript", "iframe"]
    )

    # 5. Clean residual UI noise tokens
    ui_noise = [
        r"Accordion is (?:closed|open)[^\n.]*\.",
        r"Click to (?:expand|collapse)[^\n.]*\.",
        r"Shopping Cart Click to see cart items",
        r"Search icon Click to search",
        r"Menu icon|Close icon|Caret (?:down|up|right|left) icon",
        r"<util:I18n[^>]*>",
        r"\b(?:Previous|Next)\s+Short Description\b",
        r"Short Description(?:\n+[A-Za-z0-9\s_-]+)+",
        r"Select Location\s+The Americas[\s\S]*?(?=(\n\n|\Z))"
    ]
    for pat in ui_noise:
        md = re.sub(pat, "", md, flags=re.IGNORECASE)

    # Clean excessive blank lines
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md


# -----------------------------------------------------------------------------
# MULTI-MODAL CONTENT EXTRACTOR
# -----------------------------------------------------------------------------
def extract_multimodal_data(html_content: str, raw_markdown: str | None, url: str, base_domain: str) -> dict:
    soup = BeautifulSoup(html_content, "html.parser")
    page_title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"

    # 1. Exact Hierarchical Full Markdown Content
    markdown_content = extract_exact_structured_markdown(soup, url)

    # 2. Extract HTML Data Tables into DataFrames
    extracted_tables = []
    for idx, tbl in enumerate(soup.find_all("table"), start=1):
        try:
            df_list = pd.read_html(str(tbl))
            if df_list and not df_list[0].empty:
                df = df_list[0]
                extracted_tables.append({
                    "id": f"Table #{idx}",
                    "rows": len(df),
                    "columns": len(df.columns),
                    "dataframe": df
                })
        except Exception:
            pass

    # 3. Media Assets
    images = []
    seen_imgs = set()
    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        full_src = urljoin(url, src)
        if full_src not in seen_imgs and not full_src.startswith("data:"):
            seen_imgs.add(full_src)
            images.append({
                "src": full_src,
                "alt": img.get("alt", "").strip() or "Image Asset"
            })

    # 4. SEO & Social Metadata
    meta_info = {
        "title": page_title,
        "description": "",
        "og_title": "",
        "og_description": "",
        "og_image": "",
        "canonical": ""
    }
    for m in soup.find_all("meta"):
        name = m.get("name", "").lower()
        prop = m.get("property", "").lower()
        content = m.get("content", "").strip()
        if not content:
            continue
        if name == "description" or prop == "description":
            meta_info["description"] = content
        elif prop == "og:title":
            meta_info["og_title"] = content
        elif prop == "og:description":
            meta_info["og_description"] = content
        elif prop == "og:image":
            meta_info["og_image"] = urljoin(url, content)

    can = soup.find("link", rel="canonical")
    if can and can.get("href"):
        meta_info["canonical"] = urljoin(url, can["href"])

    # 5. Outlinks Discovery
    outlinks = []
    for a in soup.find_all("a", href=True):
        norm = normalize_target_url(a["href"], base_domain, url)
        if norm and norm not in outlinks:
            outlinks.append(norm)

    # 6. Contact Emails
    emails = list(set(re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", html_content)))
    clean_emails = [e for e in emails if not e.endswith(('.png', '.jpg', '.webp', '.js', '.svg', '.css'))]

    # 7. Code Snippets
    code_snippets = []
    for pre in soup.find_all(["pre", "code"]):
        c_txt = pre.get_text().strip()
        if len(c_txt) > 20 and "\n" in c_txt and c_txt not in code_snippets:
            code_snippets.append(c_txt)

    # Metrics
    word_count = len(re.findall(r"\w+", markdown_content))
    reading_time = max(1, round(word_count / 220))

    return {
        "url": url,
        "title": page_title,
        "markdown": markdown_content,
        "metadata": meta_info,
        "word_count": word_count,
        "reading_time_min": reading_time,
        "tables": extracted_tables,
        "images": images,
        "emails": clean_emails,
        "code_snippets": code_snippets,
        "links": outlinks,
        "content_length": len(markdown_content)
    }


# -----------------------------------------------------------------------------
# ENGINE RUNNERS
# -----------------------------------------------------------------------------
async def crawl_with_crawl4ai(seed_url: str, max_pages: int, max_depth: int, progress_bar, status_text) -> list[dict]:
    parsed = urlparse(seed_url)
    base_domain = parsed.netloc

    browser_config = BrowserConfig(
        headless=True,
        browser_type="chromium",
        extra_args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
    )

    crawler_run_config = CrawlerRunConfig(
        session_id="adaptive_enterprise_session",
        cache_mode=CacheMode.BYPASS,
        wait_until="domcontentloaded",
        page_timeout=30000,
        remove_overlay_elements=True,
        word_count_threshold=5,
        js_code="window.scrollTo(0, document.body.scrollHeight/2);"
    )

    frontier: list[FrontierItem] = []
    visited = set()
    results = []

    visited.add(seed_url)
    heapq.heappush(frontier, FrontierItem(100.0, seed_url, 0))

    async with AsyncWebCrawler(config=browser_config) as crawler:
        while frontier and len(results) < max_pages:
            item = heapq.heappop(frontier)
            if status_text:
                status_text.text(f"⚡ [Crawl4AI Engine] Fetching (Score: {item.priority:.1f}, Depth: {item.depth}): {item.url[:60]}...")

            start_t = time.time()
            res = await crawler.arun(url=item.url, config=crawler_run_config)
            duration = round(time.time() - start_t, 2)

            if res.success:
                page_data = extract_multimodal_data(res.html or "", res.markdown or "", item.url, base_domain)
                page_data["status"] = "SUCCESS"
                page_data["fetch_time_sec"] = duration
                results.append(page_data)

                if item.depth < max_depth:
                    for link in page_data["links"]:
                        if link not in visited:
                            visited.add(link)
                            score = calculate_url_priority(link, item.depth + 1)
                            heapq.heappush(frontier, FrontierItem(score, link, item.depth + 1))

            if progress_bar:
                progress_bar.progress(len(results) / max_pages)

    return results


async def crawl_with_curl_cffi(seed_url: str, max_pages: int, max_depth: int, progress_bar, status_text) -> list[dict]:
    parsed = urlparse(seed_url)
    base_domain = parsed.netloc

    frontier: list[FrontierItem] = []
    visited = set()
    results = []

    visited.add(seed_url)
    heapq.heappush(frontier, FrontierItem(100.0, seed_url, 0))

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with CffiAsyncSession(impersonate="chrome124", headers=headers, timeout=25) as session:
        while frontier and len(results) < max_pages:
            item = heapq.heappop(frontier)
            if status_text:
                status_text.text(f"🚀 [Fast TLS Engine] Fetching (Score: {item.priority:.1f}, Depth: {item.depth}): {item.url[:60]}...")

            start_t = time.time()
            try:
                resp = await session.get(item.url)
                duration = round(time.time() - start_t, 2)
                if resp.status_code == 200:
                    page_data = extract_multimodal_data(resp.text, None, item.url, base_domain)
                    page_data["status"] = "SUCCESS"
                    page_data["fetch_time_sec"] = duration
                    results.append(page_data)

                    if item.depth < max_depth:
                        for link in page_data["links"]:
                            if link not in visited:
                                visited.add(link)
                                score = calculate_url_priority(link, item.depth + 1)
                                heapq.heappush(frontier, FrontierItem(score, link, item.depth + 1))
            except Exception as e:
                st.warning(f"Error fetching {item.url}: {e}")

            if progress_bar:
                progress_bar.progress(len(results) / max_pages)

    return results


# -----------------------------------------------------------------------------
# MAIN STREAMLIT UI
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Adaptive Frontier Web Extractor</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Advanced Priority-Queue Frontier Engine with multi-modal structured extraction.</div>', unsafe_allow_html=True)

# Engine Status banner
if CRAWL4AI_AVAILABLE:
    st.success("✅ **Crawl4AI Dynamic Engine Ready** (Playwright Browser + Dynamic JS Execution)")
else:
    st.info("⚡ **High-Speed Stealth TLS Engine Ready** (`curl_cffi` Chrome124 Fingerprint).")

col1, col2 = st.columns([5, 1])
with col1:
    target_url = st.text_input(
        "Target URL",
        value="https://quotes.toscrape.com/js/",
        placeholder="https://example.com",
        label_visibility="collapsed"
    )
with col2:
    start_btn = st.button("🚀 Start Engine", type="primary", use_container_width=True)

if start_btn and target_url:
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    progress = st.progress(0.0)
    status = st.empty()
    status.text("Initializing crawler session...")

    start_total_t = time.time()
    crawled_data = []

    # Execute with Crawl4AI if available, with graceful fallback to Fast TLS
    if CRAWL4AI_AVAILABLE:
        try:
            crawled_data = asyncio.run(
                crawl_with_crawl4ai(
                    seed_url=target_url,
                    max_pages=1,
                    max_depth=1,
                    progress_bar=progress,
                    status_text=status
                )
            )
        except Exception as err:
            status.text("⚡ Fallback: Executing via High-Speed TLS Engine...")
            crawled_data = asyncio.run(
                crawl_with_curl_cffi(
                    seed_url=target_url,
                    max_pages=1,
                    max_depth=1,
                    progress_bar=progress,
                    status_text=status
                )
            )
    else:
        crawled_data = asyncio.run(
            crawl_with_curl_cffi(
                seed_url=target_url,
                max_pages=1,
                max_depth=1,
                progress_bar=progress,
                status_text=status
            )
        )

    total_duration = round(time.time() - start_total_t, 2)
    status.text(f"✅ Extraction finished in {total_duration}s")

    st.markdown("---")

    if crawled_data:
        page = crawled_data[0]
        st.markdown(f"## {page['title']}")
        st.caption(f"🔗 Source: [{page['url']}]({page['url']}) | ⏱️ Fetched in {page.get('fetch_time_sec', 0)}s")

        # Top Metric Cards
        m1, m2, m3, m4, m5 = st.columns(5)
        with m1:
            st.metric("Total Words", f"{page['word_count']:,}")
        with m2:
            st.metric("Reading Time", f"~{page['reading_time_min']} min")
        with m3:
            st.metric("Data Tables", len(page["tables"]))
        with m4:
            st.metric("Images Found", len(page["images"]))
        with m5:
            st.metric("Discovered Links", len(page["links"]))

        st.markdown("---")

        # Organized Feature Tabs
        tab_text, tab_tables, tab_media, tab_seo, tab_contacts, tab_code, tab_json, tab_links = st.tabs([
            "📄 Full Text Content",
            f"📊 Tables ({len(page['tables'])})",
            f"🖼️ Media & Images ({len(page['images'])})",
            "🏷️ SEO & Metadata",
            f"📞 Contacts ({len(page['emails'])})",
            f"💻 Code ({len(page['code_snippets'])})",
            "📦 Structured JSON",
            f"🔗 Links ({len(page['links'])})"
        ])

        with tab_text:
            st.markdown(page["markdown"])

        with tab_tables:
            if page["tables"]:
                for tbl in page["tables"]:
                    st.markdown(f"#### {tbl['id']} ({tbl['rows']} rows × {tbl['columns']} cols)")
                    st.dataframe(tbl["dataframe"], use_container_width=True)
            else:
                st.info("No HTML data tables found on this page.")

        with tab_media:
            if page["images"]:
                cols = st.columns(3)
                for i, img in enumerate(page["images"][:30]):
                    with cols[i % 3]:
                        st.image(img["src"], caption=img["alt"][:35], use_container_width=True)
                        st.caption(f"🔗 [View Source]({img['src']})")
            else:
                st.info("No images found.")

        with tab_seo:
            meta = page["metadata"]
            c1, c2 = st.columns(2)
            with c1:
                st.write("**Title:**", meta.get("title", "N/A"))
                st.write("**Description:**", meta.get("description", "N/A"))
                st.write("**Canonical:**", meta.get("canonical", "N/A"))
            with c2:
                st.write("**OpenGraph Title:**", meta.get("og_title", "N/A"))
                st.write("**OpenGraph Description:**", meta.get("og_description", "N/A"))
                if meta.get("og_image"):
                    st.image(meta["og_image"], width=300)

        with tab_contacts:
            if page["emails"]:
                st.write("#### ✉️ Extracted Email Addresses")
                for e in page["emails"]:
                    st.markdown(f"- `{e}`")
            else:
                st.info("No explicit email addresses detected.")

        with tab_code:
            if page["code_snippets"]:
                for c in page["code_snippets"]:
                    st.code(c)
            else:
                st.info("No code snippets detected.")

        with tab_json:
            clean_json = {k: v for k, v in page.items() if k != "tables"}
            st.json(clean_json)

        with tab_links:
            st.write(f"Found **{len(page['links'])}** prioritized internal domain links:")
            for l in page["links"][:60]:
                st.markdown(f"- [{l}]({l})")
    else:
        st.error("❌ Extraction failed. Please verify the URL.")

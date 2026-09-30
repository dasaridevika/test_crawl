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
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
            timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
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

try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False


# -----------------------------------------------------------------------------
# PAGE CONFIGURATION & STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Enterprise Full-Site Crawler & Data Engine",
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
        return self.priority > other.priority


def calculate_url_priority(url: str, depth: int) -> float:
    """Heuristic scoring: prioritizes content paths and penalizes depth."""
    score = 100.0 - (depth * 20.0)
    valuable_keywords = ["/product/", "/article/", "/doc/", "/data/", "/item/", "/blog/", "/news/", "/case-studies/", "/solutions/"]
    if any(k in url.lower() for k in valuable_keywords):
        score += 35.0
    utility_keywords = ["/tag/", "/page/", "/category/", "/search/", "/login", "/terms", "/privacy"]
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


# -----------------------------------------------------------------------------
# MULTI-MODAL CONTENT EXTRACTOR
# -----------------------------------------------------------------------------
def extract_editorial_markdown(html_content: str, url: str) -> str:
    """Extracts complete clean editorial copy, preserving all page sections and formatting card text properly."""
    soup = BeautifulSoup(html_content, "html.parser")

    # 1. Unpack block-level <a> tags (cards) so headlines and paragraphs don't get wrapped in giant [Title Body](url) brackets
    for a in soup.find_all("a", href=True):
        has_blocks = a.find(["h1", "h2", "h3", "h4", "h5", "h6", "div", "p"])
        txt = a.get_text(" ", strip=True)
        if has_blocks or (len(txt) > 40 and ("\n" in a.get_text() or len(txt.split()) > 6)):
            a.unwrap()

    # 2. Remove non-content structural elements
    for el in soup.find_all(["script", "style", "noscript", "svg", "iframe", "button", "form", "nav", "header", "footer"]):
        el.decompose()

    # 3. Remove carousel indicator dots, country modals, and cookie banners
    noise_matchers = [
        "cmp-carousel__indicators", "cmp-carousel__actions", "carousel-indicators", "carousel-control",
        "slider-nav", "slider-pagination", "slick-dots", "cookie", "modal", "drawer",
        "country-selector", "location-selector", "sr-only", "region-selector"
    ]
    for el in soup.find_all(lambda e: e.name not in ["html", "body"] and any(m in str(e.get("class", "")).lower() or m in str(e.get("id", "")).lower() for m in noise_matchers)):
        el.decompose()

    # 4. Resolve relative URLs to absolute URLs
    for a in soup.find_all("a", href=True):
        a["href"] = urljoin(url, a["href"])

    # 5. Extract Markdown
    body = soup.find("body") or soup
    dom_md = markdownify.markdownify(
        str(body),
        heading_style="ATX",
        bullets="-",
        strip=["script", "style", "button", "form", "nav", "svg", "img", "noscript", "iframe"]
    )

    # 6. Clean UI noise phrases
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
        dom_md = re.sub(pat, "", dom_md, flags=re.IGNORECASE)

    dom_md = re.sub(r"\n{3,}", "\n\n", dom_md).strip()

    # 7. Check if Trafilatura captures dedicated single-topic article body cleanly
    if TRAFILATURA_AVAILABLE:
        try:
            traf_md = trafilatura.extract(
                html_content,
                url=url,
                output_format="markdown",
                include_links=True,
                include_images=False,
                include_tables=True,
                favor_precision=True
            )
            if traf_md and len(traf_md.strip()) > 0.7 * len(dom_md):
                return traf_md.strip()
        except Exception:
            pass

    return dom_md


def extract_multimodal_data(html_content: str, url: str, base_domain: str) -> dict:
    soup = BeautifulSoup(html_content, "html.parser")
    page_title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"

    # 1. Clean Markdown Text (Trafilatura + DOM Hybrid)
    markdown_content = extract_editorial_markdown(html_content, url)

    # 2. Extract HTML Data Tables and Definition Lists into DataFrames
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

    for d_idx, dl in enumerate(soup.find_all("dl"), start=1):
        dts = [dt.get_text(strip=True) for dt in dl.find_all("dt")]
        dds = [dd.get_text(strip=True) for dd in dl.find_all("dd")]
        if dts and len(dts) == len(dds):
            df_dl = pd.DataFrame({"Key": dts, "Value": dds})
            extracted_tables.append({
                "id": f"Key-Value List #{d_idx}",
                "rows": len(df_dl),
                "columns": 2,
                "dataframe": df_dl
            })

    # 3. High-Quality Media Assets (Exclude 1x1 pixels, base64, and SVGs)
    images = []
    seen_imgs = set()
    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        full_src = urljoin(url, src)
        if full_src.startswith(("http://", "https://")) and full_src not in seen_imgs:
            if not any(noise in full_src.lower() for noise in ["pixel.gif", "spacer.gif", "blank.gif", "1x1"]):
                seen_imgs.add(full_src)
                images.append({
                    "src": full_src,
                    "alt": img.get("alt", "").strip() or "Image Asset"
                })

    # 4. SEO, OpenGraph & JSON-LD Schemas
    meta_info = {
        "title": page_title,
        "description": "",
        "og_title": "",
        "og_description": "",
        "og_image": "",
        "canonical": "",
        "json_ld_schemas": []
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

    for s_tag in soup.find_all("script", type="application/ld+json"):
        try:
            if s_tag.string:
                parsed_schema = json.loads(s_tag.string.strip())
                meta_info["json_ld_schemas"].append(parsed_schema)
        except Exception:
            pass

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
# HIGH-CONCURRENCY ASYNC CRAWLER ENGINES
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
    visited = set([seed_url])
    results = []

    heapq.heappush(frontier, FrontierItem(100.0, seed_url, 0))

    async with AsyncWebCrawler(config=browser_config) as crawler:
        while frontier and len(results) < max_pages:
            item = heapq.heappop(frontier)
            if status_text:
                status_text.text(f"⚡ [Crawl4AI Page {len(results)+1}/{max_pages}] Fetching: {item.url[:65]}...")

            start_t = time.time()
            try:
                res = await crawler.arun(url=item.url, config=crawler_run_config)
                duration = round(time.time() - start_t, 2)

                if res.success:
                    page_data = extract_multimodal_data(res.html or "", item.url, base_domain)
                    page_data["status"] = "SUCCESS"
                    page_data["fetch_time_sec"] = duration
                    results.append(page_data)

                    if item.depth < max_depth:
                        for link in page_data["links"]:
                            if link not in visited:
                                visited.add(link)
                                score = calculate_url_priority(link, item.depth + 1)
                                heapq.heappush(frontier, FrontierItem(score, link, item.depth + 1))
            except Exception:
                pass

            if progress_bar:
                progress_bar.progress(len(results) / max_pages)

    return results


async def crawl_with_curl_cffi_concurrent(seed_url: str, max_pages: int, max_depth: int, progress_bar, status_text, concurrency: int = 5) -> list[dict]:
    parsed = urlparse(seed_url)
    base_domain = parsed.netloc

    frontier: list[FrontierItem] = []
    visited = set([seed_url])
    results = []

    heapq.heappush(frontier, FrontierItem(100.0, seed_url, 0))

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with CffiAsyncSession(impersonate="chrome124", headers=headers, timeout=25) as session:
        while frontier and len(results) < max_pages:
            batch = []
            while frontier and len(batch) < concurrency and (len(results) + len(batch)) < max_pages:
                batch.append(heapq.heappop(frontier))

            if status_text:
                status_text.text(f"🚀 [Fast Engine Batch] Crawling {len(results)}/{max_pages} pages ({len(frontier)} queued in frontier)...")

            async def fetch_item(item):
                t0 = time.time()
                try:
                    resp = await session.get(item.url)
                    dur = round(time.time() - t0, 2)
                    if resp.status_code == 200:
                        data = extract_multimodal_data(resp.text, item.url, base_domain)
                        data["status"] = "SUCCESS"
                        data["fetch_time_sec"] = dur
                        return data, item.depth
                except Exception:
                    pass
                return None, item.depth

            batch_results = await asyncio.gather(*[fetch_item(it) for it in batch])

            for res_data, depth in batch_results:
                if res_data:
                    results.append(res_data)
                    if depth < max_depth:
                        for link in res_data["links"]:
                            if link not in visited:
                                visited.add(link)
                                score = calculate_url_priority(link, depth + 1)
                                heapq.heappush(frontier, FrontierItem(score, link, depth + 1))

            if progress_bar:
                progress_bar.progress(min(1.0, len(results) / max_pages))

    return results


# -----------------------------------------------------------------------------
# MAIN STREAMLIT UI
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Enterprise Full-Site Crawler & Data Engine</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Crawl4AI Dynamic Execution + High-Concurrency Priority Frontier Full-Domain Deep Extraction.</div>', unsafe_allow_html=True)

# Engine Status Banner
if CRAWL4AI_AVAILABLE and TRAFILATURA_AVAILABLE:
    st.success("✅ **Crawl4AI Dynamic Engine Active** (Full JS Execution + Multi-Page Priority Frontier)")
elif CRAWL4AI_AVAILABLE:
    st.success("✅ **Crawl4AI Dynamic Engine Ready** (Playwright Browser + DOM Parser)")
else:
    st.info("⚡ **High-Speed Stealth TLS Engine Ready** (`curl_cffi` Chrome124 Fingerprint).")

col1, col2, col3 = st.columns([5, 3, 2])
with col1:
    target_url = st.text_input(
        "Target URL",
        value="https://quotes.toscrape.com/js/",
        placeholder="https://example.com",
        label_visibility="collapsed"
    )
with col2:
    crawl_limit = st.selectbox(
        "Crawl Depth / Scope",
        options=[1, 5, 15, 50, 100, 250],
        index=2,
        format_func=lambda x: f"📑 Crawl {x} Pages{' (Full Section)' if x == 15 else ' (Deep Site)' if x >= 50 else ''}",
        label_visibility="collapsed"
    )
with col3:
    start_btn = st.button("🚀 Start Full Crawl", type="primary", use_container_width=True)

if start_btn and target_url:
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    progress = st.progress(0.0)
    status = st.empty()
    status.text("Initializing multi-page crawler session...")

    start_total_t = time.time()
    crawled_data = []

    # Use Crawl4AI for targeted crawls (<= 15 pages) and high-concurrency engine for massive deep crawls
    if CRAWL4AI_AVAILABLE and crawl_limit <= 15:
        try:
            crawled_data = asyncio.run(
                crawl_with_crawl4ai(
                    seed_url=target_url,
                    max_pages=crawl_limit,
                    max_depth=5,
                    progress_bar=progress,
                    status_text=status
                )
            )
        except Exception:
            status.text("⚡ Fallback: Executing via High-Speed Concurrent TLS Engine...")
            crawled_data = asyncio.run(
                crawl_with_curl_cffi_concurrent(
                    seed_url=target_url,
                    max_pages=crawl_limit,
                    max_depth=5,
                    progress_bar=progress,
                    status_text=status,
                    concurrency=5
                )
            )
    else:
        crawled_data = asyncio.run(
            crawl_with_curl_cffi_concurrent(
                seed_url=target_url,
                max_pages=crawl_limit,
                max_depth=5,
                progress_bar=progress,
                status_text=status,
                concurrency=6
            )
        )

    total_duration = round(time.time() - start_total_t, 2)
    speed = round(len(crawled_data) / max(0.1, total_duration), 1)
    status.text(f"✅ Extracted {len(crawled_data)} full pages in {total_duration}s ({speed} pages/sec)")
    st.session_state["crawled_data"] = crawled_data

# Display Results from Session State
if "crawled_data" in st.session_state and st.session_state["crawled_data"]:
    crawled_data = st.session_state["crawled_data"]
    st.markdown("---")

    # Global Aggregate Metrics Across All Crawled Pages
    total_words_all = sum(p["word_count"] for p in crawled_data)
    total_tables_all = sum(len(p["tables"]) for p in crawled_data)
    total_images_all = sum(len(p["images"]) for p in crawled_data)
    total_links_all = sum(len(p["links"]) for p in crawled_data)

    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.metric("Total Pages Crawled", len(crawled_data))
    with m2:
        st.metric("Aggregate Word Count", f"{total_words_all:,}")
    with m3:
        st.metric("Total Tables Found", total_tables_all)
    with m4:
        st.metric("High-Res Images", total_images_all)
    with m5:
        st.metric("Discovered Links", total_links_all)

    st.markdown("---")

    # Export & Search Bar
    exp_col1, exp_col2 = st.columns([4, 1])
    with exp_col1:
        search_query = st.text_input("🔍 Search within all crawled pages:", placeholder="Filter by keyword (e.g. quantum, blackwell, pricing)...", label_visibility="collapsed")
    with exp_col2:
        export_payload = json.dumps([{k: v for k, v in p.items() if k != "tables"} for p in crawled_data], indent=2)
        st.download_button(
            "📦 Export Full Dataset (JSON)",
            data=export_payload,
            file_name="full_site_crawl_dataset.json",
            mime="application/json",
            use_container_width=True
        )

    # Filtered pages list
    if search_query:
        filtered_pages = [p for p in crawled_data if search_query.lower() in p["markdown"].lower() or search_query.lower() in p["title"].lower()]
        st.caption(f"Showing **{len(filtered_pages)}** pages matching '{search_query}'")
    else:
        filtered_pages = crawled_data

    # Page Selection Bar
    page_titles = [f"Page {i+1}: {p['title'][:45]} ({p['word_count']} words)" for i, p in enumerate(filtered_pages)]
    selected_idx = 0
    if len(filtered_pages) > 1:
        selected_label = st.selectbox("📂 **Select Page to Inspect:**", options=page_titles, index=0)
        selected_idx = page_titles.index(selected_label)

    if filtered_pages:
        page = filtered_pages[selected_idx]

        st.markdown(f"### {page['title']}")
        st.caption(f"🔗 URL: [{page['url']}]({page['url']}) | ⏱️ Fetched in {page.get('fetch_time_sec', 0)}s | 📝 Words: {page['word_count']:,}")

        # Multi-Modal Tabs
        tab_text, tab_all, tab_tables, tab_media, tab_seo, tab_contacts, tab_code, tab_json, tab_links = st.tabs([
            "📄 Page Text Content",
            f"📚 All {len(crawled_data)} Pages Combined",
            f"📊 Tables & Data ({len(page['tables'])})",
            f"🖼️ Media & Images ({len(page['images'])})",
            f"🏷️ SEO & JSON-LD ({len(page['metadata'].get('json_ld_schemas', []))})",
            f"📞 Contacts ({len(page['emails'])})",
            f"💻 Code ({len(page['code_snippets'])})",
            "📦 Structured JSON",
            f"🔗 Links ({len(page['links'])})"
        ])

        with tab_text:
            st.markdown(page["markdown"])

        with tab_all:
            st.markdown(f"## 📚 Consolidated Content from All {len(crawled_data)} Crawled Pages")
            for p_i, p_obj in enumerate(crawled_data, 1):
                with st.expander(f"📖 Page {p_i}: {p_obj['title']} ({p_obj['word_count']} words)", expanded=(p_i == 1)):
                    st.caption(f"Source: [{p_obj['url']}]({p_obj['url']})")
                    st.markdown(p_obj["markdown"])

        with tab_tables:
            if page["tables"]:
                for tbl in page["tables"]:
                    st.markdown(f"#### {tbl['id']} ({tbl['rows']} rows × {tbl['columns']} cols)")
                    st.dataframe(tbl["dataframe"], use_container_width=True)
            else:
                st.info("No data tables or key-value structures detected on this page.")

        with tab_media:
            if page["images"]:
                cols = st.columns(3)
                for i, img in enumerate(page["images"][:30]):
                    with cols[i % 3]:
                        st.image(img["src"], caption=img["alt"][:40], use_container_width=True)
                        st.caption(f"🔗 [View Original]({img['src']})")
            else:
                st.info("No high-resolution images found on this page.")

        with tab_seo:
            meta = page["metadata"]
            c1, c2 = st.columns(2)
            with c1:
                st.write("**Page Title:**", meta.get("title", "N/A"))
                st.write("**Meta Description:**", meta.get("description", "N/A"))
                st.write("**Canonical URL:**", meta.get("canonical", "N/A"))
            with c2:
                st.write("**OpenGraph Title:**", meta.get("og_title", "N/A"))
                st.write("**OpenGraph Description:**", meta.get("og_description", "N/A"))
                if meta.get("og_image"):
                    st.image(meta["og_image"], width=300)

            schemas = meta.get("json_ld_schemas", [])
            if schemas:
                st.markdown("### 🧩 Structured JSON-LD Schemas")
                for s_idx, schema_obj in enumerate(schemas, 1):
                    s_type = schema_obj.get("@type", "Schema") if isinstance(schema_obj, dict) else "Schema"
                    with st.expander(f"Schema #{s_idx}: {s_type}", expanded=True):
                        st.json(schema_obj)

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

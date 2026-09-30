import asyncio
import heapq
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

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
    .page-banner { background: #f1f5f9; border-left: 4px solid #2563eb; padding: 12px 16px; border-radius: 4px; margin-bottom: 16px; }
    </style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# PRIORITY FRONTIER & URL NORMALIZATION
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
    """Prioritizes content paths (articles, docs, products) over administrative links."""
    score = 100.0 - (depth * 2.0)
    valuable_keywords = ["/product/", "/article/", "/doc/", "/data/", "/item/", "/blog/", "/news/", "/case-studies/", "/solutions/", "/press-releases/"]
    if any(k in url.lower() for k in valuable_keywords):
        score += 35.0
    utility_keywords = ["/tag/", "/page/", "/category/", "/search/", "/login", "/terms", "/privacy", "/cookie"]
    if any(k in url.lower() for k in utility_keywords):
        score -= 20.0
    return score


def get_root_domain(netloc: str) -> str:
    """Extracts apex root domain (e.g. 'nvidia.com' from 'www.nvidia.com' or 'blogs.nvidia.com')."""
    parts = netloc.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return netloc.lower()


def sanitize_url(raw_url: str) -> str:
    """Strips tracking query parameters (utm_*, gclid, etc.) and anchor fragments."""
    parsed = urlparse(raw_url)
    clean_path = parsed.path.rstrip("/")
    if not clean_path:
        clean_path = ""
    
    # Strip tracking parameters
    if parsed.query:
        qs = parse_qs(parsed.query)
        tracking_keys = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref", "source", "session_id", "ncid"}
        filtered_qs = {k: v for k, v in qs.items() if k.lower() not in tracking_keys}
        clean_query = urlencode(filtered_qs, doseq=True) if filtered_qs else ""
    else:
        clean_query = ""

    clean_url = urlunparse((parsed.scheme, parsed.netloc, clean_path, "", clean_query, ""))
    return clean_url


def get_locale_prefix(path: str) -> str | None:
    """Identifies 2-letter or 5-letter locale codes in paths like /en-in/, /en-us/, /de-de/."""
    m = re.match(r"^/([a-z]{2}(?:-[a-z]{2})?)/", path.lower())
    if m:
        return m.group(1)
    return None


def normalize_target_url(raw_url: str, base_domain: str, current_url: str, seed_locale: str | None = None) -> str | None:
    """Normalizes URLs and enforces apex root domain boundaries across the entire site."""
    if not raw_url or raw_url.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    
    full_url = urljoin(current_url, raw_url).split("#")[0]
    full_url = sanitize_url(full_url)
    
    parsed = urlparse(full_url)
    root_base = get_root_domain(base_domain)
    target_netloc = parsed.netloc.lower()
    
    if (target_netloc == base_domain.lower() or target_netloc.endswith("." + root_base) or target_netloc == root_base) and parsed.scheme in ("http", "https"):
        # Ignore binary files and media
        if full_url.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".pdf", ".zip", ".tar", ".mp4", ".exe", ".iso", ".dmg", ".woff", ".ttf", ".css", ".js")):
            return None
        
        # If user started from a specific locale (e.g. /en-in/), avoid crawling all 40+ international language translations
        if seed_locale:
            url_loc = get_locale_prefix(parsed.path)
            if url_loc and url_loc != seed_locale:
                return None

        return full_url
    return None


# -----------------------------------------------------------------------------
# SITEMAP DISCOVERY HELPER
# -----------------------------------------------------------------------------
async def discover_sitemap_urls(session: "CffiAsyncSession", seed_url: str, base_domain: str, seed_locale: str | None) -> set[str]:
    """Attempts to discover all indexed URLs from sitemap.xml and robots.txt."""
    discovered = set()
    parsed = urlparse(seed_url)
    base_origin = f"{parsed.scheme}://{parsed.netloc}"

    candidate_sitemaps = [
        f"{base_origin}/sitemap.xml",
        f"{base_origin}/sitemap_index.xml",
        f"{base_origin}/robots.txt"
    ]

    for s_url in candidate_sitemaps[:5]:
        try:
            resp = await session.get(s_url, timeout=8)
            if resp.status_code == 200:
                if s_url.endswith(".txt"):
                    for line in resp.text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            s_target = line.split(":", 1)[1].strip()
                            candidate_sitemaps.append(s_target)
                elif "<urlset" in resp.text or "<sitemapindex" in resp.text:
                    locs = re.findall(r"<loc>(.*?)</loc>", resp.text, re.IGNORECASE)
                    for loc in locs:
                        loc = loc.strip()
                        if loc.endswith(".xml") and len(candidate_sitemaps) < 10:
                            candidate_sitemaps.append(loc)
                        else:
                            clean_loc = normalize_target_url(loc, base_domain, seed_url, seed_locale)
                            if clean_loc:
                                discovered.add(clean_loc)
        except Exception:
            continue

    return discovered


# -----------------------------------------------------------------------------
# MULTI-MODAL CONTENT EXTRACTOR
# -----------------------------------------------------------------------------
def extract_editorial_markdown(html_content: str, url: str) -> str:
    """Extracts complete clean editorial copy, unwrapping card link blocks and removing UI clutter."""
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

    # 3. Remove noise widgets (carousels, country selectors, cookie notices)
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

    # 5. Convert to clean Markdown
    body = soup.find("body") or soup
    dom_md = markdownify.markdownify(
        str(body),
        heading_style="ATX",
        bullets="-",
        strip=["script", "style", "button", "form", "nav", "svg", "img", "noscript", "iframe"]
    )

    # 6. Clean common UI noise patterns
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


def extract_multimodal_data(html_content: str, url: str, base_domain: str, seed_locale: str | None = None) -> dict:
    """Extracts text, Markdown, data tables, images, metadata, JSON-LD, emails, code, and internal links."""
    soup = BeautifulSoup(html_content, "html.parser")

    # Title extraction
    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    page_title = title_tag.get_text().strip() if title_tag else (h1_tag.get_text().strip() if h1_tag else "Untitled Page")

    # Metadata & JSON-LD Schemas
    meta_info = {
        "title": page_title,
        "description": "",
        "canonical": "",
        "og_title": "",
        "og_description": "",
        "og_image": "",
        "json_ld_schemas": []
    }

    for meta in soup.find_all("meta"):
        name = meta.get("name", "").lower()
        prop = meta.get("property", "").lower()
        content = meta.get("content", "")

        if name == "description":
            meta_info["description"] = content
        elif prop == "og:title":
            meta_info["og_title"] = content
        elif prop == "og:description":
            meta_info["og_description"] = content
        elif prop == "og:image":
            meta_info["og_image"] = urljoin(url, content)

    canon = soup.find("link", rel="canonical")
    if canon and canon.get("href"):
        meta_info["canonical"] = urljoin(url, canon["href"])

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            if s.string:
                parsed_schema = json.loads(s.string.strip())
                meta_info["json_ld_schemas"].append(parsed_schema)
        except Exception:
            pass

    # Clean editorial copy
    markdown_content = extract_editorial_markdown(html_content, url)

    # Tables extraction
    extracted_tables = []
    for i, table in enumerate(soup.find_all("table")):
        try:
            dfs = pd.read_html(str(table))
            if dfs and not dfs[0].empty:
                df = dfs[0]
                if df.shape[0] >= 1 and df.shape[1] >= 1:
                    extracted_tables.append({
                        "id": f"Table #{i+1}",
                        "rows": len(df),
                        "columns": len(df.columns),
                        "dataframe": df
                    })
        except Exception:
            pass

    # Images extraction
    images = []
    seen_img_urls = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("srcset", "").split()[0] if img.get("srcset") else None
        if src and not src.startswith("data:"):
            abs_src = urljoin(url, src)
            if abs_src not in seen_img_urls:
                seen_img_urls.add(abs_src)
                images.append({
                    "src": abs_src,
                    "alt": img.get("alt", "").strip() or "Image Asset",
                    "width": img.get("width", ""),
                    "height": img.get("height", "")
                })

    # Internal links discovery (including root-domain subdomains)
    outlinks = []
    seen_links = set()
    for a in soup.find_all("a", href=True):
        norm_url = normalize_target_url(a["href"], base_domain, url, seed_locale)
        if norm_url and norm_url not in seen_links:
            seen_links.add(norm_url)
            outlinks.append(norm_url)

    # Emails extraction
    emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", html_content)
    clean_emails = list(set([e for e in emails if not e.endswith((".png", ".jpg", ".js", ".css"))]))

    # Code snippets
    code_snippets = []
    for pre in soup.find_all(["pre", "code"]):
        c_txt = pre.get_text().strip()
        if len(c_txt) > 20 and "\n" in c_txt and c_txt not in code_snippets:
            code_snippets.append(c_txt)

    # Word count and reading time
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
# UNBOUNDED FULL-SITE ASYNC CRAWLER ENGINE (NO DEPTH & NO PAGE LIMITS)
# -----------------------------------------------------------------------------
async def crawl_entire_domain_unbounded(
    seed_url: str,
    status_placeholder,
    metric_placeholders: tuple,
    concurrency: int = 8
) -> list[dict]:
    """
    Crawls every single page across the entire domain with NO depth limit and NO page limit.
    Continuously updates live metrics in-place and exhausts the full domain frontier.
    """
    parsed = urlparse(seed_url)
    base_domain = parsed.netloc
    sanitized_seed = sanitize_url(seed_url)
    seed_locale = get_locale_prefix(parsed.path)

    m1_box, m2_box, m3_box, m4_box = metric_placeholders

    frontier: list[FrontierItem] = []
    visited: set[str] = set([sanitized_seed])
    results: list[dict] = []
    heapq.heappush(frontier, FrontierItem(100.0, sanitized_seed, 0))

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    start_time = time.time()

    async with CffiAsyncSession(impersonate="chrome124", headers=headers, timeout=25) as session:
        # Step 1: Discover sitemap URLs to preload all known site URLs into the frontier
        if status_placeholder:
            status_placeholder.markdown("🔍 **Step 1/2:** Discovering full site structure via `sitemap.xml` & `robots.txt`...")

        sitemap_urls = await discover_sitemap_urls(session, sanitized_seed, base_domain, seed_locale)
        for s_url in sitemap_urls:
            if s_url not in visited:
                visited.add(s_url)
                heapq.heappush(frontier, FrontierItem(90.0, s_url, 1))

        if status_placeholder:
            status_placeholder.markdown(f"🚀 **Step 2/2:** Crawling all pages across `{base_domain}` (No depth/page limits)...")

        # Step 2: Unbounded Concurrent Crawl Loop
        while frontier:
            # Prepare concurrent batch
            batch: list[FrontierItem] = []
            while frontier and len(batch) < concurrency:
                batch.append(heapq.heappop(frontier))

            async def fetch_page(item: FrontierItem):
                t0 = time.time()
                try:
                    resp = await session.get(item.url)
                    dur = round(time.time() - t0, 2)
                    if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "").lower():
                        data = extract_multimodal_data(resp.text, item.url, base_domain, seed_locale)
                        data["status"] = "SUCCESS"
                        data["fetch_time_sec"] = dur
                        data["depth"] = item.depth
                        return data, item.depth
                except Exception:
                    pass
                return None, item.depth

            batch_results = await asyncio.gather(*[fetch_page(it) for it in batch])

            for res_data, depth in batch_results:
                if res_data:
                    results.append(res_data)
                    # Enqueue all newly discovered internal links without any depth boundary
                    for link in res_data["links"]:
                        if link not in visited:
                            visited.add(link)
                            score = calculate_url_priority(link, depth + 1)
                            heapq.heappush(frontier, FrontierItem(score, link, depth + 1))

            # Update live Streamlit metrics in-place
            elapsed = max(0.1, round(time.time() - start_time, 1))
            speed = round(len(results) / elapsed, 1)
            total_words = sum(p["word_count"] for p in results)

            m1_box.metric("Pages Extracted", len(results))
            m2_box.metric("In Frontier Queue", len(frontier))
            m3_box.metric("Words Extracted", f"{total_words:,}")
            m4_box.metric("Speed (pages/sec)", f"{speed} p/s")

            if status_placeholder:
                status_placeholder.markdown(
                    f"⚡ **Live Crawling:** `{len(results)}` pages completed | `{len(frontier)}` remaining in queue | `{elapsed}s` elapsed"
                )

    return results


# -----------------------------------------------------------------------------
# MAIN STREAMLIT UI
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Enterprise Full-Site Crawler & Data Engine</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Unlimited Full-Domain Deep Crawler — Extracts 100% of pages, internal links, schemas, and content across the entire website.</div>', unsafe_allow_html=True)

# Status banner
if CURL_CFFI_AVAILABLE:
    st.success("✅ **High-Concurrency Full-Domain Engine Active** (Chrome124 Stealth TLS + Unlimited Frontier Traversal + Sitemap Preloader)")
else:
    st.info("⚡ Standard Engine Ready.")

col_url, col_btn = st.columns([4, 1.2])
with col_url:
    target_url = st.text_input(
        "Target Website URL",
        value="https://quotes.toscrape.com/js/",
        placeholder="https://example.com",
        label_visibility="collapsed"
    )
with col_btn:
    start_btn = st.button("🚀 Crawl Entire Website", type="primary", use_container_width=True)

# Live crawling execution container
if start_btn and target_url:
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    status_box = st.empty()
    
    # 4 distinct in-place placeholder slots for real-time metrics
    c1, c2, c3, c4 = st.columns(4)
    m1_slot = c1.empty()
    m2_slot = c2.empty()
    m3_slot = c3.empty()
    m4_slot = c4.empty()

    start_total_t = time.time()

    crawled_data = asyncio.run(
        crawl_entire_domain_unbounded(
            seed_url=target_url,
            status_placeholder=status_box,
            metric_placeholders=(m1_slot, m2_slot, m3_slot, m4_slot),
            concurrency=8
        )
    )

    total_duration = round(time.time() - start_total_t, 2)
    speed = round(len(crawled_data) / max(0.1, total_duration), 1)
    status_box.success(f"🎉 **Full Site Crawl Completed:** Extracted all {len(crawled_data)} pages across the domain in {total_duration}s ({speed} pages/sec)!")
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

    # Interactive Table of All Crawled Pages
    st.markdown("### 📋 Complete Crawled Pages Index")
    summary_df = pd.DataFrame([
        {
            "Page #": i + 1,
            "Title": p["title"][:60],
            "Words": p["word_count"],
            "Images": len(p["images"]),
            "Tables": len(p["tables"]),
            "Depth": p.get("depth", 0),
            "URL": p["url"]
        }
        for i, p in enumerate(crawled_data)
    ])
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    st.markdown("---")

    # Export & Search Bar
    exp_col1, exp_col2 = st.columns([4, 1])
    with exp_col1:
        search_query = st.text_input("🔍 Search across all crawled pages:", placeholder="Filter by keyword (e.g. quotes, Einstein, safety, pricing)...", label_visibility="collapsed")
    with exp_col2:
        export_payload = json.dumps([{k: v for k, v in p.items() if k != "tables"} for p in crawled_data], indent=2)
        st.download_button(
            "📦 Export Entire Dataset (JSON)",
            data=export_payload,
            file_name="complete_full_site_crawl.json",
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
    page_titles = [f"Page {i+1}: {p['title'][:55]} ({p['word_count']} words)" for i, p in enumerate(filtered_pages)]
    selected_idx = 0
    if len(filtered_pages) > 1:
        selected_label = st.selectbox("📂 **Select Page to Read / Inspect:**", options=page_titles, index=0)
        selected_idx = page_titles.index(selected_label)

    if filtered_pages:
        page = filtered_pages[selected_idx]

        st.markdown(f'<div class="page-banner"><b>Currently Reading:</b> {page["title"]}<br><small>🔗 <a href="{page["url"]}" target="_blank">{page["url"]}</a> | ⏱️ Fetched in {page.get("fetch_time_sec", 0)}s | 📝 {page["word_count"]:,} words</small></div>', unsafe_allow_html=True)

        # Multi-Modal Tabs
        tab_text, tab_all, tab_tables, tab_media, tab_seo, tab_contacts, tab_code, tab_json, tab_links = st.tabs([
            f"📄 Read Page #{selected_idx+1} Content",
            f"📚 All {len(crawled_data)} Pages Full Text",
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
            st.markdown(f"## 📚 Consolidated Full-Text of All {len(crawled_data)} Crawled Pages")
            for p_i, p_obj in enumerate(crawled_data, 1):
                st.markdown(f"--- \n### 📖 Page {p_i}: {p_obj['title']}")
                st.caption(f"🔗 Source: [{p_obj['url']}]({p_obj['url']}) | 📝 {p_obj['word_count']} words")
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

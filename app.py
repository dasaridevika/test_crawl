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
    page_title="Enterprise Parallel Web Crawler & Data Engine",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
    .main-header { font-size: 2.2rem; font-weight: 700; margin-bottom: 0.2rem; }
    .sub-header { color: #888; font-size: 0.95rem; margin-bottom: 1.2rem; }
    .stMarkdown { font-size: 1.05rem; line-height: 1.75; }
    .page-banner { background: #f8fafc; border-left: 4px solid #2563eb; padding: 14px 18px; border-radius: 6px; margin-bottom: 16px; }
    .spotlight-card { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 12px; margin-bottom: 12px; }
    </style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# PRIORITY FRONTIER & URL SANITIZATION
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
    """Prioritizes content paths (articles, blogs, news, docs, case studies) over navigation."""
    score = 100.0 - (depth * 2.0)
    valuable_keywords = ["/blog/", "/news/", "/article/", "/case-studies/", "/solutions/", "/product/", "/doc/", "/press-releases/"]
    if any(k in url.lower() for k in valuable_keywords):
        score += 50.0
    utility_keywords = ["/tag/", "/page/", "/category/", "/search/", "/login", "/terms", "/privacy", "/cookie", "/contact"]
    if any(k in url.lower() for k in utility_keywords):
        score -= 25.0
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
    
    if parsed.query:
        qs = parse_qs(parsed.query)
        tracking_keys = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref", "source", "session_id", "ncid", "trk"}
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
    if not raw_url or raw_url.startswith(("#", "javascript:", "mailto:", "tel:", "whatsapp:")):
        return None
    
    full_url = urljoin(current_url, raw_url).split("#")[0]
    full_url = sanitize_url(full_url)
    
    parsed = urlparse(full_url)
    root_base = get_root_domain(base_domain)
    target_netloc = parsed.netloc.lower()
    
    if (target_netloc == base_domain.lower() or target_netloc.endswith("." + root_base) or target_netloc == root_base) and parsed.scheme in ("http", "https"):
        if full_url.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".pdf", ".zip", ".tar", ".mp4", ".exe", ".iso", ".dmg", ".woff", ".woff2", ".ttf", ".css", ".js", ".ico")):
            return None
        
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
    """Attempts to discover indexed URLs from sitemap.xml and robots.txt."""
    discovered = set()
    parsed = urlparse(seed_url)
    base_origin = f"{parsed.scheme}://{parsed.netloc}"

    candidate_sitemaps = [
        f"{base_origin}/sitemap.xml",
        f"{base_origin}/sitemap_index.xml",
        f"{base_origin}/robots.txt"
    ]

    for s_url in candidate_sitemaps[:4]:
        try:
            resp = await session.get(s_url, timeout=5)
            if resp.status_code == 200:
                if s_url.endswith(".txt"):
                    for line in resp.text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            s_target = line.split(":", 1)[1].strip()
                            candidate_sitemaps.append(s_target)
                elif "<urlset" in resp.text or "<sitemapindex" in resp.text:
                    locs = re.findall(r"<loc>(.*?)</loc>", resp.text, re.IGNORECASE)
                    for loc in locs[:1000]:
                        loc = loc.strip()
                        if loc.endswith(".xml") and len(candidate_sitemaps) < 6:
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
    """Extracts clean formatted article or structured portal copy."""
    # 1. Primary extractor for dedicated single-topic articles / blogs
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
            if traf_md and len(traf_md.strip()) > 350:
                return traf_md.strip()
        except Exception:
            pass

    # 2. Fallback to clean DOM transformer for portals, indices, and product pages
    soup = BeautifulSoup(html_content, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "button", "form", "iframe"]):
        tag.decompose()

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("data:") or "1x1" in src:
            img.decompose()

    for el in soup.find_all(class_=re.compile(r"indicator|tab-nav|slider-nav|sr-only|cookie|modal|drawer", re.I)):
        el.decompose()

    for a in soup.find_all("a", href=True):
        has_blocks = a.find(["h1", "h2", "h3", "h4", "h5", "h6", "div", "p"])
        txt = a.get_text(" ", strip=True)
        if has_blocks or (len(txt) > 40 and ("\n" in a.get_text() or len(txt.split()) > 6)):
            a.unwrap()
        else:
            a["href"] = urljoin(url, a["href"])

    dom_md = markdownify.markdownify(
        str(soup.find("body") or soup),
        heading_style="ATX",
        bullets="-"
    )

    dom_md = re.sub(r"!\[.*?\]\(data:.*?\)", "", dom_md)
    dom_md = re.sub(r"Accordion is (?:closed|open)[^\n.]*\.", "", dom_md, flags=re.IGNORECASE)
    dom_md = re.sub(r"Click to (?:expand|collapse)[^\n.]*\.", "", dom_md, flags=re.IGNORECASE)
    dom_md = re.sub(r"\n{3,}", "\n\n", dom_md).strip()
    return dom_md


def extract_multimodal_data(html_content: str, url: str, base_domain: str, seed_locale: str | None = None) -> dict:
    """Extracts text, Markdown, data tables, images, metadata, JSON-LD, emails, code, and internal links."""
    soup = BeautifulSoup(html_content, "html.parser")

    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    page_title = title_tag.get_text().strip() if title_tag else (h1_tag.get_text().strip() if h1_tag else "Untitled Page")

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

    markdown_content = extract_editorial_markdown(html_content, url)
    raw_text_dump = "\n\n".join([p.strip() for p in soup.stripped_strings if len(p.strip()) > 3])

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

    images = []
    seen_img_urls = set()
    for img in soup.find_all(["img", "picture", "source"]):
        src = (
            img.get("src") or
            img.get("data-src") or
            img.get("data-original") or
            img.get("data-lazy-src") or
            img.get("data-url") or
            (img.get("srcset", "").split()[0] if img.get("srcset") else None)
        )
        if src and not src.startswith("data:"):
            abs_src = urljoin(url, src)
            if abs_src not in seen_img_urls and not abs_src.lower().endswith((".svg", ".ico", "1x1.gif", "spacer.gif")):
                seen_img_urls.add(abs_src)
                images.append({
                    "src": abs_src,
                    "alt": img.get("alt", "").strip() or "Image Asset",
                    "width": img.get("width", ""),
                    "height": img.get("height", "")
                })

    outlinks = []
    seen_links = set()
    for a in soup.find_all("a", href=True):
        norm_url = normalize_target_url(a["href"], base_domain, url, seed_locale)
        if norm_url and norm_url not in seen_links:
            seen_links.add(norm_url)
            outlinks.append(norm_url)

    emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", html_content)
    clean_emails = list(set([e for e in emails if not e.endswith((".png", ".jpg", ".js", ".css"))]))

    code_snippets = []
    for pre in soup.find_all(["pre", "code"]):
        c_txt = pre.get_text().strip()
        if len(c_txt) > 20 and "\n" in c_txt and c_txt not in code_snippets:
            code_snippets.append(c_txt)

    word_count = len(re.findall(r"\w+", markdown_content))
    reading_time = max(1, round(word_count / 220))

    return {
        "url": url,
        "title": page_title,
        "markdown": markdown_content,
        "raw_text": raw_text_dump,
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
# HIGH-SPEED PARALLEL WORKER POOL ENGINE
# -----------------------------------------------------------------------------
async def crawl_entire_domain_parallel_turbo(
    seed_url: str,
    status_placeholder,
    metric_placeholders: tuple,
    target_limit: int = 100,
    concurrency: int = 32
) -> list[dict]:
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

    async with CffiAsyncSession(impersonate="chrome124", headers=headers, timeout=12) as session:
        if status_placeholder:
            status_placeholder.markdown("🔍 **Preloading indexed pages** via `sitemap.xml` & `robots.txt`...")

        sitemap_urls = await discover_sitemap_urls(session, sanitized_seed, base_domain, seed_locale)
        for s_url in sitemap_urls:
            if s_url not in visited:
                visited.add(s_url)
                heapq.heappush(frontier, FrontierItem(90.0, s_url, 1))

        if status_placeholder:
            status_placeholder.markdown(f"🚀 **Parallel Turbo Crawling Active** ({concurrency} parallel workers fetching top pages)...")

        while frontier and len(results) < target_limit:
            batch: list[FrontierItem] = []
            while frontier and len(batch) < concurrency and (len(results) + len(batch)) < target_limit:
                batch.append(heapq.heappop(frontier))

            if not batch:
                break

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
                    for link in res_data["links"]:
                        if link not in visited:
                            visited.add(link)
                            score = calculate_url_priority(link, depth + 1)
                            heapq.heappush(frontier, FrontierItem(score, link, depth + 1))

            elapsed = max(0.1, round(time.time() - start_time, 1))
            speed = round(len(results) / elapsed, 1)
            total_words = sum(p["word_count"] for p in results)

            m1_box.metric("Pages Extracted", f"{len(results)} / {target_limit}")
            m2_box.metric("In Queue", len(frontier))
            m3_box.metric("Words Extracted", f"{total_words:,}")
            m4_box.metric("Speed", f"{speed} pages/sec")

            if status_placeholder:
                status_placeholder.markdown(
                    f"⚡ **Parallel Extraction:** `{len(results)}/{target_limit}` pages (`{total_words:,}` words) | `{elapsed}s` elapsed ({speed} p/s)"
                )

    return results


# -----------------------------------------------------------------------------
# MAIN STREAMLIT UI
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Enterprise Parallel Web Crawler & Data Engine</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Turbo Multi-Worker Parallel Engine — High-speed deep site extraction delivering results in seconds.</div>', unsafe_allow_html=True)

# Engine status banner
if CURL_CFFI_AVAILABLE:
    st.success("✅ **32-Stream Parallel Turbo Engine Active** (Chrome124 Stealth TLS + HTTP/2 Multiplexing + Instant Extraction)")
else:
    st.info("⚡ Standard Engine Ready.")

col_url, col_scope, col_btn = st.columns([3.5, 2.2, 1.5])
with col_url:
    target_url = st.text_input(
        "Target Website URL",
        value="https://www.nvidia.com/en-in/",
        placeholder="https://example.com",
        label_visibility="collapsed"
    )
with col_scope:
    crawl_preset = st.selectbox(
        "Crawl Scope",
        options=[25, 100, 300, 1000],
        index=1,
        format_func=lambda n: f"⚡ {n} Pages (~{3 if n<=25 else 12 if n<=100 else 30 if n<=300 else 90}s) {'(Fast)' if n==25 else '(Recommended)' if n==100 else '(Deep)'}",
        label_visibility="collapsed"
    )
with col_btn:
    start_btn = st.button("⚡ Start Fast Crawl", type="primary", use_container_width=True)

# Live crawling execution container
if start_btn and target_url:
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    status_box = st.empty()
    
    c1, c2, c3, c4 = st.columns(4)
    m1_slot = c1.empty()
    m2_slot = c2.empty()
    m3_slot = c3.empty()
    m4_slot = c4.empty()

    start_total_t = time.time()

    crawled_data = asyncio.run(
        crawl_entire_domain_parallel_turbo(
            seed_url=target_url,
            status_placeholder=status_box,
            metric_placeholders=(m1_slot, m2_slot, m3_slot, m4_slot),
            target_limit=crawl_preset,
            concurrency=32
        )
    )

    total_duration = round(time.time() - start_total_t, 2)
    speed = round(len(crawled_data) / max(0.1, total_duration), 1)
    status_box.success(f"🎉 **Crawl Completed in {total_duration}s!** Extracted {len(crawled_data)} pages ({sum(p['word_count'] for p in crawled_data):,} words) at {speed} pages/sec.")
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

    # Interactive Spotlight / Long-Form Articles Bar
    rich_articles = sorted([p for p in crawled_data if p["word_count"] > 400], key=lambda x: x["word_count"], reverse=True)
    if rich_articles:
        st.markdown("### 🔥 Top In-Depth Full Articles Discovered")
        st.caption(f"Found **{len(rich_articles)}** detailed articles/documentation pages ({sum(p['word_count'] for p in rich_articles):,} total words):")
        
        top_cols = st.columns(min(4, len(rich_articles)))
        for i, top_p in enumerate(rich_articles[:4]):
            with top_cols[i]:
                st.markdown(f"""
                <div class="spotlight-card">
                    <b>📄 {top_p['title'][:40]}</b><br>
                    <small>📝 <b>{top_p['word_count']:,} words</b> | ⏱️ {top_p.get('fetch_time_sec', 0)}s</small><br>
                    <small>🔗 <a href="{top_p['url']}" target="_blank">Open Live URL</a></small>
                </div>
                """, unsafe_allow_html=True)

    # Interactive Table of All Crawled Pages
    st.markdown("### 📋 Complete Crawled Pages Index")
    summary_df = pd.DataFrame([
        {
            "Page #": i + 1,
            "Type": "📖 Full Article" if p["word_count"] > 500 else "📑 Directory/Page",
            "Title": p["title"][:60],
            "Words": p["word_count"],
            "Images": len(p["images"]),
            "Tables": len(p["tables"]),
            "URL": p["url"]
        }
        for i, p in enumerate(crawled_data)
    ])
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    st.markdown("---")

    # Export & Search Bar
    exp_col1, exp_col2 = st.columns([4, 1])
    with exp_col1:
        search_query = st.text_input("🔍 Search across all crawled pages:", placeholder="Filter by keyword (e.g. quantum, blackwell, safety, agents)...", label_visibility="collapsed")
    with exp_col2:
        export_payload = json.dumps([{k: v for k, v in p.items() if k != "tables"} for p in crawled_data], indent=2)
        st.download_button(
            "📦 Export Full Dataset (JSON)",
            data=export_payload,
            file_name="turbo_crawl_dataset.json",
            mime="application/json",
            use_container_width=True
        )

    # Filtered pages list
    if search_query:
        filtered_pages = [p for p in crawled_data if search_query.lower() in p["markdown"].lower() or search_query.lower() in p["title"].lower()]
        st.caption(f"Showing **{len(filtered_pages)}** pages matching '{search_query}'")
    else:
        filtered_pages = crawled_data

    # Smart Page Selector: Default to highest word-count article if available, otherwise Page 1
    default_idx = 0
    if rich_articles and rich_articles[0] in filtered_pages:
        default_idx = filtered_pages.index(rich_articles[0])

    page_titles = [f"{'📖 [Article]' if p['word_count']>500 else '📑 [Page]'} #{i+1}: {p['title'][:50]} ({p['word_count']:,} w)" for i, p in enumerate(filtered_pages)]
    selected_idx = default_idx
    if len(filtered_pages) > 1:
        selected_label = st.selectbox("📂 **Select Page or Full Article to Read:**", options=page_titles, index=default_idx)
        selected_idx = page_titles.index(selected_label)

    if filtered_pages:
        page = filtered_pages[selected_idx]

        st.markdown(f'<div class="page-banner"><b>Currently Reading:</b> {page["title"]}<br><small>🔗 <a href="{page["url"]}" target="_blank">{page["url"]}</a> | ⏱️ Fetched in {page.get("fetch_time_sec", 0)}s | 📝 <b>{page["word_count"]:,} words</b></small></div>', unsafe_allow_html=True)

        # Multi-Modal Tabs
        tab_text, tab_raw, tab_paginated, tab_tables, tab_media, tab_seo, tab_contacts, tab_code, tab_json, tab_links = st.tabs([
            f"📄 Full Article / Page ({page['word_count']:,} w)",
            "📝 Raw Lossless Text",
            "📚 Multi-Page Reader",
            f"📊 Tables & Data ({len(page['tables'])})",
            f"🖼️ Images ({len(page['images'])})",
            f"🏷️ SEO & JSON-LD ({len(page['metadata'].get('json_ld_schemas', []))})",
            f"📞 Contacts ({len(page['emails'])})",
            f"💻 Code ({len(page['code_snippets'])})",
            "📦 Structured JSON",
            f"🔗 Links ({len(page['links'])})"
        ])

        with tab_text:
            st.markdown(page["markdown"])

        with tab_raw:
            st.markdown("### 📝 Lossless Raw Body Text Dump")
            st.text_area("Full Unprocessed Text Content", value=page.get("raw_text", page["markdown"]), height=500)

        with tab_paginated:
            st.markdown("### 📚 Multi-Page Article Reader (Browse 10 Pages at a time)")
            reader_page = st.number_input("Browse Batch:", min_value=1, max_value=max(1, (len(crawled_data) + 9) // 10), value=1)
            start_p = (reader_page - 1) * 10
            end_p = min(len(crawled_data), start_p + 10)
            
            for p_i in range(start_p, end_p):
                p_obj = crawled_data[p_i]
                with st.expander(f"📖 Page #{p_i+1}: {p_obj['title']} ({p_obj['word_count']} words)", expanded=(p_i == start_p)):
                    st.caption(f"🔗 Source: [{p_obj['url']}]({p_obj['url']}) | ⏱️ {p_obj.get('fetch_time_sec', 0)}s")
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

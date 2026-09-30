import asyncio
import heapq
import io
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import httpx
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
import markdownify

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION & STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Enterprise Web Crawler & Structured Dataset Engine",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
    .main-header { font-size: 2.2rem; font-weight: 700; margin-bottom: 0.2rem; }
    .sub-header { color: #888; font-size: 0.95rem; margin-bottom: 1.2rem; }
    .record-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; margin-bottom: 16px; }
    .badge-category { background: #dbeafe; color: #1e40af; padding: 4px 10px; border-radius: 4px; font-weight: 600; font-size: 0.85rem; }
    .meta-label { color: #64748b; font-size: 0.85rem; font-weight: 600; text-transform: uppercase; }
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
async def discover_sitemap_urls(client: httpx.AsyncClient, seed_url: str, base_domain: str, seed_locale: str | None) -> set[str]:
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
            resp = await client.get(s_url, timeout=5.0)
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
# STRUCTURED DATASET EXTRACTOR
# -----------------------------------------------------------------------------
def infer_category(url: str, title: str) -> str:
    """Categorizes page based on URL structure and content."""
    url_l = url.lower()
    if any(k in url_l for k in ["/blog", "/stories", "/post/"]):
        return "Blog / Article"
    if any(k in url_l for k in ["/news", "/press-releases", "/announcements"]):
        return "News & Press"
    if any(k in url_l for k in ["/docs", "/documentation", "/guide", "/api", "/learn"]):
        return "Documentation & Technical"
    if any(k in url_l for k in ["/case-studies", "/customers"]):
        return "Case Study"
    if any(k in url_l for k in ["/products", "/solutions", "/services", "/geforce", "/rtx"]):
        return "Product / Solution"
    if url.rstrip("/").count("/") <= 3 or "home" in title.lower():
        return "Homepage / Portal"
    return "Web Page"


def extract_structured_record(html_content: str, url: str, base_domain: str, seed_locale: str | None = None) -> dict:
    """Extracts a clean, tabular dataset record with Title, Summary, Author, Date, Full Text, Headings, Images, and Links."""
    soup = BeautifulSoup(html_content, "html.parser")

    # 1. Title Extraction
    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    page_title = title_tag.get_text().strip() if title_tag else (h1_tag.get_text().strip() if h1_tag else "Untitled Page")
    page_title = re.sub(r"\s+", " ", page_title).strip()

    # 2. Metadata Extraction
    meta_desc = ""
    meta_author = ""
    meta_date = ""
    og_image = ""
    json_ld_schemas = []

    for meta in soup.find_all("meta"):
        name = meta.get("name", "").lower()
        prop = meta.get("property", "").lower()
        content = meta.get("content", "").strip()

        if name in ["description", "twitter:description"] or prop in ["og:description"]:
            if not meta_desc:
                meta_desc = content
        elif name in ["author", "article:author", "dc.creator", "byl"] or prop in ["og:author"]:
            if not meta_author:
                meta_author = content
        elif name in ["article:published_time", "pubdate", "date", "dc.date"] or prop in ["article:published_time"]:
            if not meta_date:
                meta_date = content
        elif prop in ["og:image", "twitter:image"]:
            if not og_image:
                og_image = urljoin(url, content)

    # 3. JSON-LD Schemas Extraction
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            if s.string:
                parsed_schema = json.loads(s.string.strip())
                json_ld_schemas.append(parsed_schema)
                if isinstance(parsed_schema, dict):
                    if not meta_author and "author" in parsed_schema:
                        auth = parsed_schema["author"]
                        meta_author = auth.get("name", "") if isinstance(auth, dict) else str(auth)
                    if not meta_date and "datePublished" in parsed_schema:
                        meta_date = str(parsed_schema["datePublished"])
                    if not meta_desc and "description" in parsed_schema:
                        meta_desc = str(parsed_schema["description"])
        except Exception:
            pass

    # 4. Fallback Author & Date from DOM tags
    if not meta_author:
        author_el = soup.find(class_=re.compile(r"author|byline|post-author|writer", re.I))
        if author_el:
            meta_author = author_el.get_text(" ", strip=True)

    if not meta_date:
        time_el = soup.find("time") or soup.find(class_=re.compile(r"date|publish|timestamp", re.I))
        if time_el:
            meta_date = time_el.get("datetime") or time_el.get_text(" ", strip=True)

    # 5. Extract Headings & Key Topics Hierarchy
    headings = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        h_text = h.get_text(" ", strip=True)
        if len(h_text) > 3 and len(h_text) < 120 and h_text not in headings:
            headings.append(h_text)

    # 6. Extract Images
    images = []
    seen_imgs = set()
    for img in soup.find_all(["img", "picture", "source"]):
        src = (
            img.get("src") or
            img.get("data-src") or
            img.get("data-original") or
            img.get("data-lazy-src") or
            (img.get("srcset", "").split()[0] if img.get("srcset") else None)
        )
        if src and not src.startswith("data:"):
            abs_src = urljoin(url, src)
            if abs_src not in seen_imgs and not abs_src.lower().endswith((".svg", ".ico", "1x1.gif", "spacer.gif")):
                seen_imgs.add(abs_src)
                images.append(abs_src)

    hero_image = og_image if og_image else (images[0] if images else "")

    # 7. Extract Internal Links
    outlinks = []
    seen_links = set()
    for a in soup.find_all("a", href=True):
        norm_url = normalize_target_url(a["href"], base_domain, url, seed_locale)
        if norm_url and norm_url not in seen_links:
            seen_links.add(norm_url)
            outlinks.append(norm_url)

    # 8. Clean Editorial Full Body Text (Lossless without script/style/nav clutter)
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "button", "form", "select", "option"]):
        tag.decompose()

    for el in soup.find_all(class_=re.compile(r"cookie|modal|drawer|newsletter-popup|banner-cookie", re.I)):
        el.decompose()

    # Paragraphs extraction for summary & clean prose
    paragraphs = []
    for p in soup.find_all(["p", "li"]):
        txt = p.get_text(" ", strip=True)
        if len(txt) > 25:
            paragraphs.append(txt)

    full_body_text = "\n\n".join(paragraphs) if paragraphs else "\n\n".join([s.strip() for s in soup.stripped_strings if len(s.strip()) > 20])
    
    # Summary calculation if meta_desc is missing
    if not meta_desc and paragraphs:
        meta_desc = paragraphs[0][:250] + ("..." if len(paragraphs[0]) > 250 else "")

    word_count = len(re.findall(r"\w+", full_body_text))
    reading_time = max(1, round(word_count / 220))
    category = infer_category(url, page_title)

    return {
        "Title": page_title,
        "Category": category,
        "Summary": meta_desc or "No description available",
        "Author": meta_author or "N/A",
        "Date": meta_date or "N/A",
        "Word Count": word_count,
        "Reading Time (min)": reading_time,
        "Key Topics": ", ".join(headings[:8]) if headings else "N/A",
        "Hero Image": hero_image,
        "Total Images": len(images),
        "Total Links": len(outlinks),
        "Full Body Text": full_body_text,
        "All Images": images,
        "Discovered Links": outlinks,
        "JSON-LD Schemas": json_ld_schemas,
        "URL": url
    }


# -----------------------------------------------------------------------------
# STANDARD DIRECT HTTP ASYNC CRAWLER (NO BROWSER IMPERSONATION)
# -----------------------------------------------------------------------------
async def crawl_structured_dataset_direct(
    seed_url: str,
    status_placeholder,
    metric_placeholders: tuple,
    target_limit: int = 100,
    concurrency: int = 24
) -> list[dict]:
    """
    Standard, transparent async HTTP client crawler using HTTP/2 connection pooling with zero browser impersonation.
    """
    parsed = urlparse(seed_url)
    base_domain = parsed.netloc
    sanitized_seed = sanitize_url(seed_url)
    seed_locale = get_locale_prefix(parsed.path)

    m1_box, m2_box, m3_box, m4_box = metric_placeholders

    frontier: list[FrontierItem] = []
    visited: set[str] = set([sanitized_seed])
    records: list[dict] = []
    heapq.heappush(frontier, FrontierItem(100.0, sanitized_seed, 0))

    # Standard transparent crawler headers
    headers = {
        "User-Agent": "EnterpriseDataEngine/1.0 (+https://example.com/bot; Web Data Extraction Engine)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }

    limits = httpx.Limits(max_connections=50, max_keepalive_connections=30)
    start_time = time.time()

    async with httpx.AsyncClient(headers=headers, http2=True, timeout=12.0, limits=limits, follow_redirects=True) as client:
        if status_placeholder:
            status_placeholder.markdown("🔍 **Preloading indexed domain pages** via `sitemap.xml` & `robots.txt`...")

        sitemap_urls = await discover_sitemap_urls(client, sanitized_seed, base_domain, seed_locale)
        for s_url in sitemap_urls:
            if s_url not in visited:
                visited.add(s_url)
                heapq.heappush(frontier, FrontierItem(90.0, s_url, 1))

        if status_placeholder:
            status_placeholder.markdown(f"🚀 **Direct HTTP Parallel Extraction Active** ({concurrency} async streams)...")

        while frontier and len(records) < target_limit:
            batch: list[FrontierItem] = []
            while frontier and len(batch) < concurrency and (len(records) + len(batch)) < target_limit:
                batch.append(heapq.heappop(frontier))

            if not batch:
                break

            async def fetch_page(item: FrontierItem):
                t0 = time.time()
                try:
                    resp = await client.get(item.url)
                    dur = round(time.time() - t0, 2)
                    if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "").lower():
                        data = extract_structured_record(resp.text, item.url, base_domain, seed_locale)
                        data["fetch_time_sec"] = dur
                        data["depth"] = item.depth
                        return data, item.depth
                except Exception:
                    pass
                return None, item.depth

            batch_results = await asyncio.gather(*[fetch_page(it) for it in batch])

            for res_data, depth in batch_results:
                if res_data:
                    records.append(res_data)
                    for link in res_data["Discovered Links"]:
                        if link not in visited:
                            visited.add(link)
                            score = calculate_url_priority(link, depth + 1)
                            heapq.heappush(frontier, FrontierItem(score, link, depth + 1))

            elapsed = max(0.1, round(time.time() - start_time, 1))
            speed = round(len(records) / elapsed, 1)
            total_words = sum(p["Word Count"] for p in records)

            m1_box.metric("Pages Extracted", f"{len(records)} / {target_limit}")
            m2_box.metric("In Frontier Queue", len(frontier))
            m3_box.metric("Total Words", f"{total_words:,}")
            m4_box.metric("Extraction Speed", f"{speed} pages/sec")

            if status_placeholder:
                status_placeholder.markdown(
                    f"⚡ **Extracted `{len(records)}/{target_limit}` Structured Records** (`{total_words:,}` words) | `{elapsed}s` elapsed ({speed} p/s)"
                )

    return records


# -----------------------------------------------------------------------------
# MAIN STREAMLIT UI
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Enterprise Web Crawler & Structured Dataset Engine</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Direct Standard HTTP Crawler — Extracts clean structured records without browser impersonation, exportable to Excel, CSV, and JSON.</div>', unsafe_allow_html=True)

st.success("✅ **Standard Direct HTTP Client Active** (`httpx` HTTP/2 Connection Pool — Zero Browser Impersonation)")

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
    start_btn = st.button("⚡ Extract Dataset", type="primary", use_container_width=True)

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

    dataset_records = asyncio.run(
        crawl_structured_dataset_direct(
            seed_url=target_url,
            status_placeholder=status_box,
            metric_placeholders=(m1_slot, m2_slot, m3_slot, m4_slot),
            target_limit=crawl_preset,
            concurrency=24
        )
    )

    total_duration = round(time.time() - start_total_t, 2)
    speed = round(len(dataset_records) / max(0.1, total_duration), 1)
    status_box.success(f"🎉 **Dataset Extraction Completed in {total_duration}s!** Extracted {len(dataset_records)} structured records ({sum(p['Word Count'] for p in dataset_records):,} total words) at {speed} pages/sec.")
    st.session_state["dataset_records"] = dataset_records


# Display Results from Session State
if "dataset_records" in st.session_state and st.session_state["dataset_records"]:
    dataset_records = st.session_state["dataset_records"]
    st.markdown("---")

    # Global Aggregate Metrics Across Dataset
    total_words_all = sum(p["Word Count"] for p in dataset_records)
    total_images_all = sum(p["Total Images"] for p in dataset_records)
    total_links_all = sum(p["Total Links"] for p in dataset_records)
    article_count = sum(1 for p in dataset_records if p["Category"] in ["Blog / Article", "News & Press", "Case Study"])

    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.metric("Total Records Extracted", len(dataset_records))
    with m2:
        st.metric("Articles & News", article_count)
    with m3:
        st.metric("Total Words Extracted", f"{total_words_all:,}")
    with m4:
        st.metric("Total Images Discovered", f"{total_images_all:,}")
    with m5:
        st.metric("Total Links Mapped", f"{total_links_all:,}")

    st.markdown("---")

    # 📥 DATASET EXPORT SUITE (EXCEL, CSV, JSON)
    st.markdown("### 📥 Download Structured Dataset")
    
    tabular_df = pd.DataFrame([
        {
            "Title": r["Title"],
            "Category": r["Category"],
            "Author": r["Author"],
            "Date": r["Date"],
            "Word Count": r["Word Count"],
            "Reading Time (min)": r["Reading Time (min)"],
            "Summary": r["Summary"],
            "Key Topics": r["Key Topics"],
            "Hero Image": r["Hero Image"],
            "Total Images": r["Total Images"],
            "Total Links": r["Total Links"],
            "URL": r["URL"],
            "Full Body Text": r["Full Body Text"]
        }
        for r in dataset_records
    ])

    exp_c1, exp_c2, exp_c3 = st.columns(3)
    
    with exp_c1:
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            tabular_df.to_excel(writer, index=False, sheet_name="Crawled_Dataset")
        excel_data = excel_buffer.getvalue()
        
        st.download_button(
            "📗 Download Excel Dataset (.xlsx)",
            data=excel_data,
            file_name="crawled_structured_dataset.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    with exp_c2:
        csv_data = tabular_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📄 Download CSV Dataset (.csv)",
            data=csv_data,
            file_name="crawled_structured_dataset.csv",
            mime="text/csv",
            use_container_width=True
        )

    with exp_c3:
        json_payload = json.dumps(dataset_records, indent=2)
        st.download_button(
            "📦 Download JSON Dataset (.json)",
            data=json_payload,
            file_name="crawled_structured_dataset.json",
            mime="application/json",
            use_container_width=True
        )

    st.markdown("---")

    # 📊 INTERACTIVE DATASET TABLE
    st.markdown("### 📊 Interactive Dataset Grid")
    
    search_col, cat_col = st.columns([3, 2])
    with search_col:
        search_query = st.text_input("🔍 Search Dataset:", placeholder="Search title, summary, topic, or keyword...", label_visibility="collapsed")
    with cat_col:
        categories = ["All Categories"] + sorted(list(set(r["Category"] for r in dataset_records)))
        selected_cat = st.selectbox("Filter Category:", options=categories, label_visibility="collapsed")

    filtered_df = tabular_df.copy()
    if selected_cat != "All Categories":
        filtered_df = filtered_df[filtered_df["Category"] == selected_cat]
    if search_query:
        mask = (
            filtered_df["Title"].str.contains(search_query, case=False, na=False) |
            filtered_df["Summary"].str.contains(search_query, case=False, na=False) |
            filtered_df["Key Topics"].str.contains(search_query, case=False, na=False) |
            filtered_df["Full Body Text"].str.contains(search_query, case=False, na=False)
        )
        filtered_df = filtered_df[mask]

    st.dataframe(
        filtered_df[["Title", "Category", "Author", "Date", "Word Count", "Key Topics", "URL"]],
        use_container_width=True,
        hide_index=True
    )
    st.caption(f"Showing **{len(filtered_df)}** of **{len(dataset_records)}** records")

    st.markdown("---")

    # 🔎 RECORD DETAIL INSPECTOR
    st.markdown("### 🔎 Record Detail Inspector")
    
    record_titles = [f"#{i+1} [{r['Category']}] {r['Title'][:60]} ({r['Word Count']:,} words)" for i, r in enumerate(dataset_records)]
    selected_record_label = st.selectbox("📂 **Select Record to View Details:**", options=record_titles, index=0)
    selected_record_idx = record_titles.index(selected_record_label)
    record = dataset_records[selected_record_idx]

    # Record Detail Card
    st.markdown(f"""
    <div class="record-card">
        <span class="badge-category">{record['Category']}</span>
        <h2 style="margin-top: 8px; margin-bottom: 4px;">{record['Title']}</h2>
        <p style="color: #475569; font-size: 1rem; margin-bottom: 12px;"><i>{record['Summary']}</i></p>
        <div style="display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 8px;">
            <div><span class="meta-label">Author:</span> {record['Author']}</div>
            <div><span class="meta-label">Published Date:</span> {record['Date']}</div>
            <div><span class="meta-label">Word Count:</span> {record['Word Count']:,} words (~{record['Reading Time (min)']} min read)</div>
            <div><span class="meta-label">Source URL:</span> <a href="{record['URL']}" target="_blank">{record['URL']}</a></div>
        </div>
        <div><span class="meta-label">Key Topics / Headings:</span> {record['Key Topics']}</div>
    </div>
    """, unsafe_allow_html=True)

    tab_text, tab_images, tab_links, tab_json = st.tabs([
        "📄 Full Body Text Content",
        f"🖼️ Images ({record['Total Images']})",
        f"🔗 Discovered Outlinks ({record['Total Links']})",
        "📦 Raw Structured JSON"
    ])

    with tab_text:
        st.text_area("Full Body Text", value=record["Full Body Text"], height=450)

    with tab_images:
        if record["All Images"]:
            cols = st.columns(3)
            for i, img_url in enumerate(record["All Images"][:15]):
                with cols[i % 3]:
                    st.image(img_url, use_container_width=True)
                    st.caption(f"🔗 [View Image Asset]({img_url})")
        else:
            st.info("No images extracted for this record.")

    with tab_links:
        if record["Discovered Links"]:
            for l in record["Discovered Links"][:50]:
                st.markdown(f"- [{l}]({l})")
        else:
            st.info("No outlinks found.")

    with tab_json:
        st.json(record)

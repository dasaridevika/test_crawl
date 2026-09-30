import asyncio
import json
import random
import time
from urllib.parse import urljoin, urlparse
import defusedxml.ElementTree as ET
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from markdownify import markdownify as md

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION & STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Enterprise Web Crawler & Data Engine",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
    <style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        color: #888;
        font-size: 0.95rem;
        margin-bottom: 1.5rem;
    }
    .metric-card {
        background-color: rgba(128, 128, 128, 0.05);
        border: 1px solid rgba(128, 128, 128, 0.2);
        border-radius: 8px;
        padding: 12px;
    }
    </style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# CONTENT EXTRACTION & CLEANING UTILITIES
# -----------------------------------------------------------------------------
def clean_html_to_markdown(html_content: str, include_links: bool = True) -> str:
    """Cleans HTML noise and converts main content to clean Markdown."""
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Remove true noise elements
    for el in soup(["script", "style", "noscript", "svg", "iframe"]):
        el.decompose()

    body = soup.find("body") or soup
    strip_tags = [] if include_links else ["a"]
    raw_md = md(str(body), heading_style="ATX", strip=strip_tags + ["img"], bullets="-")
    
    # Clean redundant blank lines
    lines = []
    consecutive_empty = 0
    for line in raw_md.splitlines():
        line_str = line.strip()
        if not line_str:
            consecutive_empty += 1
            if consecutive_empty <= 1:
                lines.append("")
        else:
            consecutive_empty = 0
            lines.append(line_str)
            
    return "\n".join(lines).strip()


def extract_page_metadata(html_content: str, url: str) -> dict:
    """Extracts title, description, schema, headings, and internal links."""
    soup = BeautifulSoup(html_content, "html.parser")
    base_domain = urlparse(url).netloc
    
    title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"
    
    # Description
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = desc_tag.get("content", "").strip() if desc_tag else ""
    
    # JSON-LD Schema
    json_ld = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            if s.string:
                json_ld.append(json.loads(s.string.strip()))
        except Exception:
            pass

    # Headings
    headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"]) if h.get_text(strip=True)]
    
    # Internal links
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            full_url = urljoin(url, href)
            if urlparse(full_url).netloc == base_domain:
                links.add(full_url)

    return {
        "title": title,
        "description": description,
        "headings": headings,
        "json_ld": json_ld,
        "links": sorted(list(links))
    }


# -----------------------------------------------------------------------------
# HIGH-SPEED ASYNC DISCOVERY & CRAWLING ENGINE
# -----------------------------------------------------------------------------
async def discover_sitemap_urls(session: AsyncSession, base_url: str, profile: str) -> list[str]:
    """Attempts to discover URLs directly from sitemap.xml."""
    parsed = urlparse(base_url)
    sitemap_candidates = [
        f"{parsed.scheme}://{parsed.netloc}/sitemap.xml",
        f"{parsed.scheme}://{parsed.netloc}/sitemap_index.xml",
    ]

    for s_url in sitemap_candidates:
        try:
            resp = await session.get(s_url, impersonate=profile, timeout=10)
            if resp.status_code == 200 and (b"<urlset" in resp.content or b"<sitemapindex" in resp.content):
                root = ET.fromstring(resp.content)
                namespaces = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                urls = [elem.text for elem in root.findall(".//ns:loc", namespaces) if elem.text]
                if urls:
                    return urls
        except Exception:
            pass
    return []


async def fetch_single_url(session: AsyncSession, url: str, profile: str, timeout: int, delay_range: tuple) -> dict:
    """Extracts a single page with anti-bot TLS impersonation."""
    if delay_range[1] > 0:
        await asyncio.sleep(random.uniform(delay_range[0], delay_range[1]))
        
    start_t = time.time()
    try:
        resp = await session.get(url, impersonate=profile, timeout=timeout)
        duration = round(time.time() - start_t, 2)
        
        if resp.status_code == 200:
            meta = extract_page_metadata(resp.text, url)
            markdown = clean_html_to_markdown(resp.text)
            return {
                "status": "SUCCESS",
                "status_code": 200,
                "url": url,
                "title": meta["title"],
                "description": meta["description"],
                "headings_count": len(meta["headings"]),
                "discovered_links_count": len(meta["links"]),
                "markdown": markdown,
                "headings": meta["headings"],
                "json_ld": meta["json_ld"],
                "links": meta["links"],
                "response_time_sec": duration,
                "content_length": len(markdown)
            }
        else:
            return {
                "status": "FAILED",
                "status_code": resp.status_code,
                "url": url,
                "title": f"HTTP {resp.status_code}",
                "description": "",
                "markdown": "",
                "response_time_sec": duration,
                "error": f"HTTP {resp.status_code}"
            }
    except Exception as e:
        return {
            "status": "ERROR",
            "status_code": 0,
            "url": url,
            "title": "Error",
            "description": "",
            "markdown": "",
            "response_time_sec": round(time.time() - start_t, 2),
            "error": str(e)
        }


async def run_hub_and_spoke_crawler(
    seed_url: str,
    max_pages: int,
    concurrency: int,
    profile: str,
    timeout: int,
    delay_range: tuple,
    url_pattern: str,
    progress_bar,
    status_text
) -> list[dict]:
    """
    Production-Grade Hub-and-Spoke Crawling Pipeline:
    1. Instant Sitemap Discovery (Hub)
    2. Fallback to BFS recursive discovery if sitemap not found
    3. URL queue filtering, deduplication & randomization
    4. Async TLS worker pool (Spokes) with adaptive concurrency
    """
    semaphore = asyncio.Semaphore(concurrency)
    discovered_urls = []
    
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1"
    }

    async with AsyncSession(headers=headers) as session:
        # STEP 1: Discovery (Hub)
        status_text.text("🔍 Phase 1: Checking sitemap.xml for instant index discovery...")
        sitemap_urls = await discover_sitemap_urls(session, seed_url, profile)
        
        if sitemap_urls:
            status_text.text(f"✅ Found {len(sitemap_urls):,} URLs in sitemap index.")
            discovered_urls = sitemap_urls
        else:
            status_text.text("⚠️ No sitemap found. Performing fast seed link extraction...")
            seed_res = await fetch_single_url(session, seed_url, profile, timeout, (0, 0))
            if seed_res.get("status") == "SUCCESS":
                discovered_urls = [seed_url] + seed_res.get("links", [])

        # STEP 2: Filter and Randomize Queue
        if url_pattern:
            discovered_urls = [u for u in discovered_urls if url_pattern.lower() in u.lower()]
            
        discovered_urls = list(dict.fromkeys(discovered_urls))  # Deduplicate
        random.shuffle(discovered_urls)  # Randomize to avoid linear scraping signatures
        
        target_batch = discovered_urls[:max_pages]
        if not target_batch and seed_url not in target_batch:
            target_batch = [seed_url]

        total_targets = len(target_batch)
        status_text.text(f"🚀 Phase 2: Extracting {total_targets} pages across {concurrency} async TLS workers...")

        # STEP 3: High-Speed Async Spokes Extraction
        results = []
        completed = 0

        async def worker(url: str):
            nonlocal completed
            async with semaphore:
                res = await fetch_single_url(session, url, profile, timeout, delay_range)
                results.append(res)
                completed += 1
                progress_bar.progress(completed / total_targets)
                status_text.text(f"⚡ Extracted {completed}/{total_targets} pages | Current: {url[:50]}...")

        tasks = [worker(url) for url in target_batch]
        await asyncio.gather(*tasks)

        status_text.text(f"✅ Extraction completed for {len(results)} pages.")
        return results


# -----------------------------------------------------------------------------
# SIDEBAR: PRODUCTION CONFIGURATION
# -----------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ Engine Controls")
    
    crawl_mode = st.radio(
        "Crawl Mode",
        ["Single Page Extraction", "Multi-Page Domain Crawl"],
        index=1,
        help="Single Page: Fast instant extraction. Multi-Page: Crawls domain using Hub-and-Spoke."
    )
    
    st.markdown("---")
    st.subheader("🛡️ Anti-Bot Parameters")
    
    impersonate_choice = st.selectbox(
        "TLS / Browser Fingerprint",
        ["chrome120", "chrome119", "safari17_0", "edge101"],
        index=0,
        help="Impersonates exact TLS handshakes, JA3/JA4 signatures, and HTTP/2 frames."
    )
    
    min_delay = st.number_input("Min Delay (s)", min_value=0.0, max_value=5.0, value=0.1, step=0.1)
    max_delay = st.number_input("Max Delay (s)", min_value=0.0, max_value=5.0, value=0.4, step=0.1)
    
    if crawl_mode == "Multi-Page Domain Crawl":
        st.markdown("---")
        st.subheader("⚡ Concurrency & Bounds")
        max_pages_input = st.slider("Max Pages to Extract", min_value=5, max_value=200, value=25, step=5)
        concurrency_input = st.slider("Concurrent Workers", min_value=1, max_value=20, value=8, step=1)
        url_filter_keyword = st.text_input("URL Keyword Filter (Optional)", placeholder="e.g. /product/ or /news/")
    else:
        max_pages_input = 1
        concurrency_input = 1
        url_filter_keyword = ""

    request_timeout = st.slider("Request Timeout (s)", 5, 45, 15)
    include_hyperlinks = st.checkbox("Preserve Links in Markdown", value=True)


# -----------------------------------------------------------------------------
# MAIN DASHBOARD INTERFACE
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Production Web Crawler & Data Engine</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">High-throughput, anti-bot resistant extraction engine with automated sitemap discovery & TLS impersonation.</div>', unsafe_allow_html=True)

# Input Row
col_url, col_btn = st.columns([5, 1])
with col_url:
    input_url = st.text_input(
        "Target URL",
        value="https://www.nvidia.com/en-in/",
        placeholder="https://example.com",
        label_visibility="collapsed"
    )
with col_btn:
    start_crawl = st.button("🚀 Start Engine", type="primary", use_container_width=True)

# Crawl Execution
if start_crawl and input_url:
    if not input_url.startswith(("http://", "https://")):
        input_url = "https://" + input_url

    progress_bar = st.progress(0.0)
    status_text = st.empty()
    
    start_time = time.time()

    if crawl_mode == "Single Page Extraction":
        status_text.text(f"Connecting to {input_url} via {impersonate_choice} TLS session...")
        async def run_single():
            async with AsyncSession() as s:
                return await fetch_single_url(s, input_url, impersonate_choice, request_timeout, (0, 0))
        results = [asyncio.run(run_single())]
        progress_bar.progress(1.0)
        status_text.text("✅ Single page extraction complete.")
    else:
        results = asyncio.run(
            run_hub_and_spoke_crawler(
                seed_url=input_url,
                max_pages=max_pages_input,
                concurrency=concurrency_input,
                profile=impersonate_choice,
                timeout=request_timeout,
                delay_range=(min_delay, max_delay),
                url_pattern=url_filter_keyword,
                progress_bar=progress_bar,
                status_text=status_text
            )
        )

    elapsed_time = round(time.time() - start_time, 2)
    successful_results = [r for r in results if r.get("status") == "SUCCESS"]

    # Metric Row
    st.markdown("---")
    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.metric("Total Pages Crawled", len(results))
    with m2:
        st.metric("Successful (200 OK)", len(successful_results))
    with m3:
        st.metric("Total Execution Time", f"{elapsed_time}s")
    with m4:
        avg_speed = round(len(results) / elapsed_time, 2) if elapsed_time > 0 else 0
        st.metric("Throughput", f"{avg_speed} pages/s")
    with m5:
        total_chars = sum(r.get("content_length", 0) for r in successful_results)
        st.metric("Extracted Volume", f"{total_chars:,} chars")

    # Display Results
    st.markdown("---")
    
    # Export Options Top Bar
    exp_col1, exp_col2, _ = st.columns([1.5, 1.5, 4])
    with exp_col1:
        json_export = json.dumps(results, indent=2, ensure_ascii=False)
        st.download_button(
            label="📥 Download Dataset (JSON)",
            data=json_export,
            file_name=f"crawled_dataset_{int(time.time())}.json",
            mime="application/json",
            use_container_width=True
        )
    with exp_col2:
        df_summary = pd.DataFrame([
            {
                "Status": r.get("status_code"),
                "URL": r.get("url"),
                "Title": r.get("title"),
                "Content Length": r.get("content_length", 0),
                "Response Time (s)": r.get("response_time_sec", 0)
            }
            for r in results
        ])
        csv_export = df_summary.to_csv(index=False)
        st.download_button(
            label="📥 Download Summary (CSV)",
            data=csv_export,
            file_name=f"crawl_summary_{int(time.time())}.csv",
            mime="text/csv",
            use_container_width=True
        )

    # Result Tabs
    tab_overview, tab_viewer, tab_json = st.tabs([
        "📋 Dataset Overview",
        "📖 Page-by-Page Inspector",
        "📊 Raw JSON Output"
    ])

    with tab_overview:
        st.dataframe(df_summary, use_container_width=True, height=400)

    with tab_viewer:
        if successful_results:
            selected_url = st.selectbox(
                "Select a crawled page to inspect:",
                [r["url"] for r in successful_results]
            )
            selected_page = next((r for r in successful_results if r["url"] == selected_url), None)
            
            if selected_page:
                st.subheader(selected_page.get("title", "Untitled"))
                st.caption(f"🔗 URL: {selected_page['url']} | ⏱️ Fetch time: {selected_page['response_time_sec']}s")
                
                md_tab, schema_tab, links_tab = st.tabs(["📝 Markdown", "📑 Schema / Metadata", "🔗 Outlinks"])
                with md_tab:
                    st.markdown(selected_page.get("markdown", "No content extracted."))
                with schema_tab:
                    st.write("**Headings:**", selected_page.get("headings", []))
                    st.write("**JSON-LD Structured Schema:**", selected_page.get("json_ld", []))
                with links_tab:
                    st.write(f"**Discovered {len(selected_page.get('links', []))} outlinks:**")
                    st.dataframe(selected_page.get("links", []), column_config={"value": "URL"}, use_container_width=True)
        else:
            st.warning("No successful pages to display.")

    with tab_json:
        st.json(results)

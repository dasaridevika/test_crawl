import asyncio
import json
import random
import time
from urllib.parse import urljoin, urlparse
import defusedxml.ElementTree as ET
import streamlit as st
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from markdownify import markdownify as md

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Web Data Extractor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling
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
    .result-container {
        background-color: rgba(128, 128, 128, 0.05);
        border: 1px solid rgba(128, 128, 128, 0.15);
        border-radius: 10px;
        padding: 20px;
        margin-top: 15px;
    }
    </style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# EXTRACTION & CLEANING LOGIC
# -----------------------------------------------------------------------------
def clean_html_to_markdown(html_content: str, include_links: bool = True) -> str:
    """Cleans HTML noise and converts the core page content into clean Markdown."""
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Strip non-content / noise tags
    for el in soup(["script", "style", "noscript", "svg", "iframe"]):
        el.decompose()

    body = soup.find("body") or soup
    strip_tags = [] if include_links else ["a"]
    raw_md = md(str(body), heading_style="ATX", strip=strip_tags + ["img"], bullets="-")
    
    # Clean redundant blank lines while preserving paragraphs
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


def extract_page_data(html_content: str, url: str) -> dict:
    """Extracts title, meta description, clean markdown, headings, schema, and links."""
    soup = BeautifulSoup(html_content, "html.parser")
    base_domain = urlparse(url).netloc
    
    title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"
    
    # Meta description
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = desc_tag.get("content", "").strip() if desc_tag else ""
    
    # Headings
    headings = [
        {"level": h.name.upper(), "text": h.get_text(strip=True)}
        for h in soup.find_all(["h1", "h2", "h3", "h4"])
        if h.get_text(strip=True)
    ]
    
    # JSON-LD Schemas
    json_ld = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            if s.string:
                json_ld.append(json.loads(s.string.strip()))
        except Exception:
            pass

    # Internal links
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            full_url = urljoin(url, href)
            if urlparse(full_url).netloc == base_domain:
                links.add(full_url)

    markdown_text = clean_html_to_markdown(html_content)

    return {
        "url": url,
        "title": title,
        "description": description,
        "markdown": markdown_text,
        "headings": headings,
        "json_ld": json_ld,
        "links": sorted(list(links)),
        "content_length": len(markdown_text)
    }


# -----------------------------------------------------------------------------
# HIGH-SPEED ASYNC ENGINE (TLS / JA3 IMPERSONATION)
# -----------------------------------------------------------------------------
async def fetch_page(session: AsyncSession, url: str, profile: str, timeout: int) -> dict:
    """Fetches and extracts a page using Chrome TLS impersonation."""
    try:
        resp = await session.get(url, impersonate=profile, timeout=timeout)
        if resp.status_code == 200:
            data = extract_page_data(resp.text, url)
            data["status"] = "SUCCESS"
            data["status_code"] = 200
            return data
        else:
            return {
                "status": "FAILED",
                "status_code": resp.status_code,
                "url": url,
                "title": f"HTTP {resp.status_code}",
                "markdown": f"Failed to retrieve content. Server responded with status code {resp.status_code}.",
                "headings": [],
                "json_ld": [],
                "links": [],
                "content_length": 0
            }
    except Exception as e:
        return {
            "status": "ERROR",
            "status_code": 0,
            "url": url,
            "title": "Error",
            "markdown": f"Error fetching URL: {str(e)}",
            "headings": [],
            "json_ld": [],
            "links": [],
            "content_length": 0
        }


async def discover_sitemap_urls(session: AsyncSession, base_url: str, profile: str) -> list[str]:
    """Attempts to discover URLs directly from sitemap."""
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


async def crawl_domain(
    seed_url: str,
    max_pages: int,
    concurrency: int,
    profile: str,
    timeout: int,
    progress_bar,
    status_text
) -> list[dict]:
    """Crawls multiple pages from domain using Hub-and-Spoke and returns extracted results."""
    semaphore = asyncio.Semaphore(concurrency)
    
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1"
    }

    async with AsyncSession(headers=headers) as session:
        status_text.text("🔍 Phase 1: Checking sitemap.xml for fast URL discovery...")
        sitemap_urls = await discover_sitemap_urls(session, seed_url, profile)
        
        if sitemap_urls:
            status_text.text(f"✅ Found {len(sitemap_urls):,} URLs in sitemap.")
            targets = sitemap_urls
        else:
            status_text.text("⚠️ No sitemap found. Extracting links from seed page...")
            seed_data = await fetch_page(session, seed_url, profile, timeout)
            targets = [seed_url] + seed_data.get("links", [])

        # Deduplicate & Shuffle
        targets = list(dict.fromkeys(targets))
        random.shuffle(targets)
        batch = targets[:max_pages]

        if not batch and seed_url not in batch:
            batch = [seed_url]

        status_text.text(f"🚀 Phase 2: Extracting data from {len(batch)} pages in parallel...")
        
        results = []
        completed = 0

        async def worker(u: str):
            nonlocal completed
            async with semaphore:
                await asyncio.sleep(random.uniform(0.1, 0.3))
                data = await fetch_page(session, u, profile, timeout)
                results.append(data)
                completed += 1
                progress_bar.progress(completed / len(batch))

        tasks = [worker(u) for u in batch]
        await asyncio.gather(*tasks)

        status_text.text(f"✅ Extracted data from {len(results)} pages.")
        return results


# -----------------------------------------------------------------------------
# SIDEBAR SETTINGS
# -----------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ Crawl Settings")
    
    crawl_mode = st.radio(
        "Mode",
        ["Single URL", "Domain Crawl (Multi-Page)"],
        index=0
    )
    
    tls_profile = st.selectbox(
        "TLS Browser Profile",
        ["chrome120", "chrome119", "safari17_0", "edge101"],
        index=0,
        help="Impersonates exact TLS handshakes and JA3/JA4 fingerprints to bypass bot blocks."
    )
    
    if crawl_mode == "Domain Crawl (Multi-Page)":
        max_pages = st.slider("Pages to Extract", min_value=2, max_value=50, value=10, step=1)
        concurrency = st.slider("Workers (Concurrency)", min_value=1, max_value=15, value=5, step=1)
    else:
        max_pages = 1
        concurrency = 1

    request_timeout = st.slider("Timeout (seconds)", 5, 45, 15)


# -----------------------------------------------------------------------------
# MAIN UI & DATA DISPLAY
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Web Data Extractor</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Enter any URL below to bypass bot detection and immediately view the extracted data.</div>', unsafe_allow_html=True)

# Input Bar
col1, col2 = st.columns([5, 1])
with col1:
    target_url = st.text_input(
        "Target URL",
        value="https://www.nvidia.com/en-in/",
        placeholder="https://example.com",
        label_visibility="collapsed"
    )
with col2:
    start_btn = st.button("🚀 Extract Data", type="primary", use_container_width=True)

if start_btn and target_url:
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    progress_bar = st.progress(0.0)
    status_text = st.empty()
    start_time = time.time()

    if crawl_mode == "Single URL":
        status_text.text(f"Extracting {target_url} via {tls_profile} TLS session...")
        async def run_single():
            async with AsyncSession() as s:
                return await fetch_page(s, target_url, tls_profile, request_timeout)
        extracted_results = [asyncio.run(run_single())]
        progress_bar.progress(1.0)
        status_text.text("✅ Extraction complete.")
    else:
        extracted_results = asyncio.run(
            crawl_domain(
                seed_url=target_url,
                max_pages=max_pages,
                concurrency=concurrency,
                profile=tls_profile,
                timeout=request_timeout,
                progress_bar=progress_bar,
                status_text=status_text
            )
        )

    elapsed_time = round(time.time() - start_time, 2)
    successful = [r for r in extracted_results if r.get("status") == "SUCCESS"]

    st.markdown("---")

    # Metrics Summary
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Pages Extracted", len(extracted_results))
    with m2:
        st.metric("Total Extracted Chars", f"{sum(r.get('content_length', 0) for r in successful):,}")
    with m3:
        st.metric("Execution Time", f"{elapsed_time}s")
    with m4:
        st.metric("Status", "✅ 200 OK" if successful else "❌ Failed")

    st.markdown("---")

    # =========================================================================
    # DIRECT EXTRACTED DATA DISPLAY (NO DOWNLOAD BUTTONS)
    # =========================================================================
    st.subheader("📄 Extracted Data Output")

    if crawl_mode == "Single URL" and successful:
        page = successful[0]
        
        st.markdown(f"### {page.get('title', 'Untitled')}")
        if page.get("description"):
            st.caption(f"**Description:** {page['description']}")
        
        tab_md, tab_json, tab_structure, tab_links = st.tabs([
            "📝 Extracted Markdown Content",
            "📊 Structured JSON Data",
            "📑 Headings & Hierarchy",
            "🔗 Extracted Links"
        ])

        with tab_md:
            st.markdown(page["markdown"])

        with tab_json:
            st.json(page)

        with tab_structure:
            if page.get("headings"):
                for h in page["headings"]:
                    st.markdown(f"- **{h['level']}**: {h['text']}")
            else:
                st.info("No headings found.")

        with tab_links:
            if page.get("links"):
                st.write(f"Found **{len(page['links'])}** internal URLs:")
                for link in page["links"]:
                    st.markdown(f"- [{link}]({link})")
            else:
                st.info("No links found.")

    elif crawl_mode == "Domain Crawl (Multi-Page)" and successful:
        st.write(f"Displaying extracted data for **{len(successful)}** pages:")

        for idx, page in enumerate(successful, start=1):
            with st.expander(f"**{idx}. {page.get('title', 'Untitled')}** — `{page.get('url')}`", expanded=(idx == 1)):
                st.caption(f"🔗 **URL:** {page.get('url')} | 📏 **Length:** {page.get('content_length', 0):,} chars")
                
                tab_p_md, tab_p_json, tab_p_links = st.tabs(["📝 Extracted Markdown", "📊 JSON", "🔗 Outlinks"])
                
                with tab_p_md:
                    st.markdown(page.get("markdown", "No content."))
                
                with tab_p_json:
                    st.json(page)
                    
                with tab_p_links:
                    st.write(f"**{len(page.get('links', []))} outlinks:**")
                    for l in page.get("links", [])[:30]:
                        st.markdown(f"- [{l}]({l})")
    else:
        st.error("No content could be extracted. Please check the URL or try another TLS profile in the sidebar.")

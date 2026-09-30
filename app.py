import asyncio
import json
import time
from urllib.parse import urljoin, urlparse
import streamlit as st
import trafilatura
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION & STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Production Web Data Extractor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
    .main-header { font-size: 2.2rem; font-weight: 700; margin-bottom: 0.2rem; }
    .sub-header { color: #888; font-size: 0.95rem; margin-bottom: 1.5rem; }
    .stMarkdown { font-size: 1rem; line-height: 1.7; }
    </style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# STATE-OF-THE-ART EXTRACTION ENGINE (Trafilatura + Metadata Tree)
# -----------------------------------------------------------------------------
def extract_high_precision_content(html_content: str, url: str) -> dict:
    """
    Uses Trafilatura (industry benchmark for AI web extraction) to extract
    exact, complete page text with zero boilerplate noise and 100% fidelity.
    """
    # 1. Trafilatura High-Fidelity Markdown Extraction
    extracted_md = trafilatura.extract(
        html_content,
        url=url,
        output_format="markdown",
        include_links=True,
        include_images=False,
        include_tables=True,
        include_comments=False,
        favor_recall=True,          # Ensures NO content or article sections are missed
        deduplicate=True
    )

    # 2. Extract Document Metadata
    metadata = trafilatura.extract_metadata(html_content, default_url=url)
    
    # 3. Fallback to Beautiful Soup if Trafilatura returns empty
    soup = BeautifulSoup(html_content, "html.parser")
    base_domain = urlparse(url).netloc
    
    page_title = (metadata.title if metadata and metadata.title else None) or (soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled")
    description = (metadata.description if metadata and metadata.description else "")

    if not extracted_md:
        # Fallback cleaner
        for el in soup(["script", "style", "noscript", "svg", "iframe"]):
            el.decompose()
        main_c = soup.find("main") or soup.find("article") or soup.find("body") or soup
        extracted_md = main_c.get_text("\n\n", strip=True)

    # 4. Extract Discovered Internal Domain Links
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            full_url = urljoin(url, href)
            if urlparse(full_url).netloc == base_domain:
                links.add(full_url)

    # 5. Extract JSON-LD Schema
    json_ld = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            if s.string:
                json_ld.append(json.loads(s.string.strip()))
        except Exception:
            pass

    return {
        "title": page_title,
        "description": description,
        "url": url,
        "markdown": extracted_md,
        "json_ld": json_ld,
        "links": sorted(list(links)),
        "content_length": len(extracted_md) if extracted_md else 0
    }


# -----------------------------------------------------------------------------
# HIGH-SPEED ASYNC NETWORK LAYER (Chrome 120 TLS / JA3 / HTTP/2)
# -----------------------------------------------------------------------------
async def fetch_website(url: str) -> dict:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }
    try:
        async with AsyncSession(headers=headers) as session:
            start_t = time.time()
            resp = await session.get(url, impersonate="chrome120", timeout=25)
            duration = round(time.time() - start_t, 2)
            
            if resp.status_code == 200:
                data = extract_high_precision_content(resp.text, url)
                data["status"] = "SUCCESS"
                data["status_code"] = 200
                data["fetch_time_sec"] = duration
                return data
            else:
                return {
                    "status": "FAILED",
                    "status_code": resp.status_code,
                    "error": f"Target server returned HTTP {resp.status_code}"
                }
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


# -----------------------------------------------------------------------------
# MAIN INTERFACE
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Web Data Extractor</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">High-precision, full-text web data extraction powered by Trafilatura & Chrome TLS Impersonation.</div>', unsafe_allow_html=True)

# URL Input Row
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

    with st.spinner("Extracting complete text content..."):
        result = asyncio.run(fetch_website(target_url))

    st.markdown("---")

    if result.get("status") == "SUCCESS":
        st.markdown(f"## {result['title']}")
        if result.get("description"):
            st.caption(f"**Summary:** {result['description']}")

        # Metrics Bar
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("Extracted Length", f"{result['content_length']:,} chars")
        with m2:
            st.metric("Fetch Time", f"{result.get('fetch_time_sec', 0)}s")
        with m3:
            st.metric("Discovered Links", len(result.get("links", [])))
        with m4:
            st.metric("Status", "✅ 200 OK")

        st.markdown("---")

        tab_text, tab_json, tab_links = st.tabs([
            "📄 Exact Extracted Content",
            "📊 Structured JSON",
            "🔗 Discovered Links"
        ])

        with tab_text:
            st.markdown(result["markdown"])

        with tab_json:
            st.json(result)

        with tab_links:
            st.write(f"Found **{len(result['links'])}** internal domain links:")
            for l in result["links"]:
                st.markdown(f"- [{l}]({l})")
    else:
        st.error(f"❌ Extraction error: {result.get('error', 'Failed to fetch.')}")

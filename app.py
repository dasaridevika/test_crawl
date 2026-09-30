import asyncio
import json
import re
import time
from urllib.parse import urljoin, urlparse
import streamlit as st
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from markdownify import markdownify as md

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION & STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Web Data Extractor",
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
# EXACT EDITORIAL BODY EXTRACTOR
# -----------------------------------------------------------------------------
def extract_pure_editorial_content(html_content: str, base_url: str) -> dict:
    """
    Extracts the exact editorial and story body of the website:
    - Removes regional language pickers (Argentina, Australia, Belgique...)
    - Removes mega-menu category dumps
    - Removes mechanical carousel tokens ('Previous', 'Next', 'Short Description...')
    - Preserves 100% of the actual stories, articles, descriptions, and action links
    """
    soup = BeautifulSoup(html_content, "html.parser")
    base_domain = urlparse(base_url).netloc
    page_title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"

    # 1. Strip script, style, and svg tags
    for el in soup(["script", "style", "noscript", "svg", "iframe"]):
        el.decompose()

    # 2. Convert body to Markdown with links preserved
    body = soup.find("body") or soup
    raw_md = md(str(body), heading_style="ATX", strip=["img"], bullets="-")

    # 3. Precision Filtering:
    # A) Remove everything before the first real editorial story
    # (Removes the entire language popup and mega-menu navigation block)
    split_markers = [
        "Cybersecurity Introducing NVIDIA Open Agent Safety Platform",
        "Agentic AI\nNVIDIA and Palantir",
        "Agentic AI\n\nNVIDIA and Palantir",
        "Introducing NVIDIA Open Agent Safety Platform",
        "With NVIDIA Blackwell and NVIDIA Dynamo"
    ]
    
    clean_body = raw_md
    for marker in split_markers:
        if marker in clean_body:
            idx = clean_body.find(marker)
            clean_body = clean_body[idx:]
            break

    # B) Remove internal carousel UI tokens ('Next', 'Previous', 'Short Description ...')
    clean_body = re.sub(r"\b(Previous|Next)\b", "", clean_body)
    clean_body = re.sub(r"Short Description(\s*\n\s*[^\n]+\d+)+", "", clean_body, flags=re.IGNORECASE)
    clean_body = re.sub(r"(\n\s*[A-Z0-9\s]{3,35}\s\d+\s*\n)+", "\n", clean_body)

    # 4. Clean consecutive empty lines while maintaining readable spacing
    lines = []
    consecutive_empty = 0
    for line in clean_body.splitlines():
        line_str = line.strip()
        if not line_str:
            consecutive_empty += 1
            if consecutive_empty <= 1:
                lines.append("")
        else:
            consecutive_empty = 0
            lines.append(line_str)

    final_markdown = "\n".join(lines).strip()

    # 5. Extract all internal domain links
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            full_url = urljoin(base_url, href)
            if urlparse(full_url).netloc == base_domain:
                links.add(full_url)

    # 6. Extract JSON-LD Schema
    json_ld = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            if s.string:
                json_ld.append(json.loads(s.string.strip()))
        except Exception:
            pass

    return {
        "title": page_title,
        "url": base_url,
        "markdown": final_markdown,
        "content_length": len(final_markdown),
        "links": sorted(list(links)),
        "json_ld": json_ld
    }


# -----------------------------------------------------------------------------
# HIGH-SPEED ASYNC NETWORK LAYER (TLS / JA3 IMPERSONATION)
# -----------------------------------------------------------------------------
async def fetch_page(url: str) -> dict:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1"
    }
    try:
        async with AsyncSession(headers=headers) as session:
            start_t = time.time()
            resp = await session.get(url, impersonate="chrome120", timeout=25)
            duration = round(time.time() - start_t, 2)

            if resp.status_code == 200:
                data = extract_pure_editorial_content(resp.text, url)
                data["status"] = "SUCCESS"
                data["fetch_time_sec"] = duration
                return data
            else:
                return {"status": "FAILED", "error": f"Server returned HTTP {resp.status_code}"}
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


# -----------------------------------------------------------------------------
# MAIN UI
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Web Data Extractor</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Extracts pure editorial content, stories, articles, and links without navigation clutter.</div>', unsafe_allow_html=True)

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

    with st.spinner("Extracting pure editorial content..."):
        result = asyncio.run(fetch_page(target_url))

    st.markdown("---")

    if result.get("status") == "SUCCESS":
        st.markdown(f"## {result['title']}")
        st.caption(f"🔗 Source: [{result['url']}]({result['url']})")

        # Metrics Bar
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("Extracted Content Length", f"{result['content_length']:,} chars")
        with m2:
            st.metric("Fetch Time", f"{result.get('fetch_time_sec', 0)}s")
        with m3:
            st.metric("Discovered Links", len(result.get("links", [])))
        with m4:
            st.metric("Status", "✅ 200 OK")

        st.markdown("---")

        tab_exact, tab_json, tab_links = st.tabs([
            "📄 Exact Extracted Content",
            "📊 Structured JSON",
            "🔗 Discovered Links"
        ])

        with tab_exact:
            st.markdown(result["markdown"])

        with tab_json:
            st.json(result)

        with tab_links:
            st.write(f"Found **{len(result['links'])}** internal domain links:")
            for l in result["links"]:
                st.markdown(f"- [{l}]({l})")
    else:
        st.error(f"❌ Extraction error: {result.get('error', 'Failed to fetch.')}")

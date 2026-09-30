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
    </style>
""", unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# EXTRACTION & CLEANING UTILITIES
# -----------------------------------------------------------------------------
def clean_html_to_markdown(html_content: str) -> str:
    """Cleans HTML noise and converts page content into clean Markdown."""
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Strip non-content / noise tags
    for el in soup(["script", "style", "noscript", "svg", "iframe"]):
        el.decompose()

    body = soup.find("body") or soup
    raw_md = md(str(body), heading_style="ATX", strip=["img"], bullets="-")
    
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
    
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = desc_tag.get("content", "").strip() if desc_tag else ""
    
    headings = [
        {"level": h.name.upper(), "text": h.get_text(strip=True)}
        for h in soup.find_all(["h1", "h2", "h3", "h4"])
        if h.get_text(strip=True)
    ]
    
    json_ld = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            if s.string:
                json_ld.append(json.loads(s.string.strip()))
        except Exception:
            pass

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
# HIGH-SPEED ASYNC ENGINE (AUTOMATIC ANTI-BOT IMPERSONATION)
# -----------------------------------------------------------------------------
async def fetch_page(session: AsyncSession, url: str) -> dict:
    """Fetches and extracts a page using automatic modern Chrome TLS impersonation."""
    try:
        resp = await session.get(url, impersonate="chrome120", timeout=20)
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
                "markdown": f"Failed to retrieve content. Server returned HTTP {resp.status_code}.",
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


# -----------------------------------------------------------------------------
# MAIN UI & DATA DISPLAY
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Web Data Extractor</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Enter any URL to extract data with automatic bot detection bypass.</div>', unsafe_allow_html=True)

# URL Input Bar
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

    with st.spinner("Extracting data..."):
        async def run():
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
                "Upgrade-Insecure-Requests": "1"
            }
            async with AsyncSession(headers=headers) as session:
                return await fetch_page(session, target_url)

        page_data = asyncio.run(run())

    st.markdown("---")

    if page_data.get("status") == "SUCCESS":
        st.markdown(f"### {page_data.get('title', 'Untitled')}")
        if page_data.get("description"):
            st.caption(f"**Description:** {page_data['description']}")
        
        # Metrics
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("Total Extracted Chars", f"{page_data['content_length']:,}")
        with m2:
            st.metric("Headings Count", len(page_data.get("headings", [])))
        with m3:
            st.metric("Discovered Links", len(page_data.get("links", [])))

        st.markdown("---")

        # Tabs for Extracted Data Display
        tab_md, tab_json, tab_structure, tab_links = st.tabs([
            "📝 Extracted Markdown",
            "📊 Structured JSON",
            "📑 Headings & Structure",
            "🔗 Discovered Links"
        ])

        with tab_md:
            st.markdown(page_data["markdown"])

        with tab_json:
            st.json(page_data)

        with tab_structure:
            if page_data.get("headings"):
                for h in page_data["headings"]:
                    st.markdown(f"- **{h['level']}**: {h['text']}")
            else:
                st.info("No headings found.")

        with tab_links:
            if page_data.get("links"):
                st.write(f"Found **{len(page_data['links'])}** internal links:")
                for link in page_data["links"]:
                    st.markdown(f"- [{link}]({link})")
            else:
                st.info("No links found.")
    else:
        st.error(f"❌ {page_data.get('markdown', 'Extraction failed.')}")

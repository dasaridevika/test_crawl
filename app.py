import asyncio
import json
import re
import streamlit as st
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from markdownify import markdownify as md
from urllib.parse import urljoin, urlparse

# Page Configuration
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
        font-size: 1rem;
        margin-bottom: 1.5rem;
    }
    .stCodeBlock {
        border-radius: 8px;
    }
    </style>
""", unsafe_allow_html=True)


def extract_comprehensive_content(html_content: str, include_links: bool = True) -> dict:
    """
    Extracts complete, comprehensive content from enterprise sites (like NVIDIA)
    without accidentally discarding nested sections, cards, and dynamic containers.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    
    # 1. Extract JSON-LD Structured Data (Enterprise schema metadata)
    json_ld_data = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if script.string:
                data = json.loads(script.string.strip())
                json_ld_data.append(data)
        except Exception:
            pass

    # 2. Extract Headings Hierarchy
    headings = []
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = h.get_text(strip=True)
        if text:
            headings.append({"tag": h.name.upper(), "text": text})

    # 3. Clean only true noise (scripts, styles, hidden tracking pixels)
    # Note: We do NOT aggressively remove header/nav/footer if they contain essential links/cards
    for element in soup(["script", "style", "noscript", "svg", "iframe"]):
        element.decompose()

    # 4. Generate High-Fidelity Markdown
    body = soup.find("body") or soup
    strip_tags = [] if include_links else ["a"]
    raw_markdown = md(
        str(body),
        heading_style="ATX",
        strip=strip_tags + ["img"],
        bullets="-"
    )
    
    # Clean redundant blank lines while preserving paragraph structure
    clean_lines = []
    consecutive_empty = 0
    for line in raw_markdown.splitlines():
        line_str = line.strip()
        if not line_str:
            consecutive_empty += 1
            if consecutive_empty <= 1:
                clean_lines.append("")
        else:
            consecutive_empty = 0
            clean_lines.append(line_str)
            
    full_markdown = "\n".join(clean_lines).strip()

    # 5. Extract all readable text blocks / paragraphs
    paragraphs = []
    for p in soup.find_all(["p", "li", "span", "div"]):
        # only leaf or direct text
        if not p.find(["p", "div"]):
            txt = p.get_text(strip=True)
            if len(txt) > 30 and txt not in paragraphs:
                paragraphs.append(txt)

    return {
        "markdown": full_markdown,
        "headings": headings,
        "paragraphs_count": len(paragraphs),
        "json_ld": json_ld_data
    }


def extract_links(html_content: str, base_url: str) -> list[str]:
    """Extracts unique internal links from the HTML page."""
    soup = BeautifulSoup(html_content, "html.parser")
    base_domain = urlparse(base_url).netloc
    links = set()
    
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            full_url = urljoin(base_url, href)
            if urlparse(full_url).netloc == base_domain:
                links.add(full_url)
            
    return sorted(list(links))


async def fetch_page_fast(url: str, profile: str = "chrome120", timeout: int = 20) -> tuple[int, str]:
    """Fetches full page with Chrome TLS/JA3/JA4 and HTTP/2 browser impersonation."""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
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
    
    async with AsyncSession(headers=headers) as session:
        resp = await session.get(url, impersonate=profile, timeout=timeout)
        return resp.status_code, resp.text


# -----------------------------------------------------------------------------
# SIDEBAR CONTROLS
# -----------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ Extraction Settings")
    
    impersonate_choice = st.selectbox(
        "Browser TLS Profile",
        ["chrome120", "chrome119", "safari17_0", "edge101"],
        index=0,
        help="Replicates the exact TLS, JA3/JA4 fingerprint and HTTP/2 frames of modern browsers."
    )
    
    include_links_in_md = st.checkbox("Include Hyperlinks in Markdown", value=True)
    extract_schema = st.checkbox("Extract JSON-LD Schema / Metadata", value=True)
    timeout_sec = st.slider("Request Timeout (sec)", 5, 45, 20)
    
    st.markdown("---")
    st.markdown("### 💡 Why Enterprise Sites (e.g. NVIDIA)")
    st.caption(
        "Enterprise websites like NVIDIA use complex dynamic modular containers. "
        "This parser extracts all headings, content blocks, cards, and JSON-LD structured schemas without dropping nested sections."
    )


# -----------------------------------------------------------------------------
# MAIN CONTENT AREA
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Web Data Extractor</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Comprehensive, anti-bot resistant data extraction for any website.</div>', unsafe_allow_html=True)

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
    extract_clicked = st.button("🚀 Extract Data", type="primary", use_container_width=True)

if extract_clicked and target_url:
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    with st.spinner(f"Extracting comprehensive data from {target_url}..."):
        try:
            status_code, html_content = asyncio.run(
                fetch_page_fast(target_url, profile=impersonate_choice, timeout=timeout_sec)
            )

            if status_code == 200:
                soup = BeautifulSoup(html_content, "html.parser")
                page_title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled Page"
                
                # Comprehensive Extraction
                extracted = extract_comprehensive_content(html_content, include_links=include_links_in_md)
                markdown_result = extracted["markdown"]
                headings_list = extracted["headings"]
                json_ld_list = extracted["json_ld"]
                discovered_links = extract_links(html_content, target_url)

                st.success(f"✅ Successfully extracted **{target_url}** (Status: 200 OK)")

                # Top Metrics
                metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
                with metric_col1:
                    st.metric("Page Title", page_title[:30] + ("..." if len(page_title) > 30 else ""))
                with metric_col2:
                    st.metric("Headings Found", len(headings_list))
                with metric_col3:
                    st.metric("Internal Links", len(discovered_links))
                with metric_col4:
                    st.metric("Extracted Length", f"{len(markdown_result):,} chars")

                st.markdown("---")

                # Result Tabs
                tab1, tab2, tab3, tab4, tab5 = st.tabs([
                    "📝 Complete Markdown", 
                    "📊 Structured JSON", 
                    "📑 Headings & Structure", 
                    "🔗 Discovered Links", 
                    "🌐 Raw HTML"
                ])

                with tab1:
                    st.download_button(
                        label="📥 Download Complete Markdown (.md)",
                        data=markdown_result,
                        file_name="nvidia_extracted_content.md",
                        mime="text/markdown"
                    )
                    st.markdown(markdown_result)

                with tab2:
                    structured_payload = {
                        "url": target_url,
                        "title": page_title,
                        "status_code": status_code,
                        "headings_count": len(headings_list),
                        "links_count": len(discovered_links),
                        "json_ld_schema": json_ld_list,
                        "headings": headings_list,
                        "content_markdown": markdown_result,
                        "links": discovered_links
                    }
                    json_str = json.dumps(structured_payload, indent=2, ensure_ascii=False)
                    st.download_button(
                        label="📥 Download Complete JSON (.json)",
                        data=json_str,
                        file_name="nvidia_extracted_data.json",
                        mime="application/json"
                    )
                    st.json(structured_payload)

                with tab3:
                    st.write(f"### Site Structure ({len(headings_list)} Headings)")
                    if headings_list:
                        for h in headings_list:
                            indent = "&nbsp;&nbsp;&nbsp;&nbsp;" * (int(h["tag"][1]) - 1)
                            st.markdown(f"{indent}**{h['tag']}**: {h['text']}", unsafe_allow_html=True)
                    else:
                        st.info("No explicit heading tags found.")

                with tab4:
                    st.write(f"Found **{len(discovered_links)}** internal links on this domain:")
                    st.dataframe(discovered_links, column_config={"value": "URL"}, use_container_width=True)

                with tab5:
                    with st.expander("View Raw HTML Source"):
                        st.code(html_content[:15000] + ("\n... [truncated]" if len(html_content) > 15000 else ""), language="html")

            elif status_code in (403, 503):
                st.error(f"⚠️ Access blocked by target server (HTTP {status_code}). Try switching the TLS profile in the sidebar.")
            else:
                st.warning(f"Target responded with HTTP {status_code}")

        except Exception as e:
            st.error(f"❌ Extraction error: {str(e)}")

import asyncio
import json
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


def clean_html_to_markdown(html_content: str) -> str:
    """
    Cleans boilerplate elements (nav, footer, ads, cookie banners, scripts)
    and converts the core HTML content into structured Markdown.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Strip non-content and noise tags
    noise_selectors = [
        "script", "style", "noscript", "svg", "iframe",
        "nav", "footer", "header", "aside",
        ".cookie-banner", "#cookie-notice", ".advertisement", ".ad-container",
        "[role='banner']", "[role='navigation']"
    ]
    for selector in noise_selectors:
        for element in soup.select(selector):
            element.decompose()

    # Prioritize main article/content container if present
    main_content = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", {"id": "content"})
        or soup.find("body")
        or soup
    )

    raw_markdown = md(str(main_content), heading_style="ATX", strip=["img", "a"])
    lines = [line.strip() for line in raw_markdown.splitlines()]
    clean_markdown = "\n".join(line for line in lines if line)
    return clean_markdown


def extract_links(html_content: str, base_url: str) -> list[str]:
    """Extracts unique internal links from the HTML page."""
    soup = BeautifulSoup(html_content, "html.parser")
    base_domain = urlparse(base_url).netloc
    links = set()
    
    for a in soup.find_all("a", href=True):
        full_url = urljoin(base_url, a["href"])
        if urlparse(full_url).netloc == base_domain:
            links.add(full_url)
            
    return sorted(list(links))


async def fetch_with_curl_cffi(url: str, impersonate_profile: str = "chrome120") -> tuple[int, str, dict]:
    """Fetches URL with full TLS/JA3 and HTTP/2 browser impersonation."""
    async with AsyncSession() as session:
        resp = await session.get(url, impersonate=impersonate_profile, timeout=15)
        return resp.status_code, resp.text, dict(resp.headers)


# -----------------------------------------------------------------------------
# SIDEBAR CONTROLS
# -----------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ Extraction Settings")
    
    impersonate_choice = st.selectbox(
        "Browser TLS Profile",
        ["chrome120", "chrome119", "safari17_0", "edge101"],
        index=0,
        help="Replicates the exact TLS, JA3/JA4 fingerprint and HTTP/2 frames of the selected browser."
    )
    
    timeout_sec = st.slider("Request Timeout (sec)", 5, 30, 15)
    
    st.markdown("---")
    st.markdown("### 🛡️ Anti-Bot Capabilities")
    st.markdown("""
    - ✅ **TLS / JA3 Impersonation**
    - ✅ **HTTP/2 Multiplexing**
    - ✅ **Modern Client Hints (`Sec-CH-UA`)**
    - ✅ **Boilerplate Noise Stripping**
    """)
    st.markdown("---")
    st.caption("Deployed with Streamlit Cloud & `curl_cffi`")


# -----------------------------------------------------------------------------
# MAIN CONTENT AREA
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Web Data Extractor</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Extract clean Markdown and structured JSON without triggering bot filters.</div>', unsafe_allow_html=True)

# URL Input Bar
col1, col2 = st.columns([5, 1])
with col1:
    target_url = st.text_input(
        "Target URL",
        value="https://quotes.toscrape.com/js/",
        placeholder="https://example.com",
        label_visibility="collapsed"
    )
with col2:
    extract_clicked = st.button("🚀 Extract Data", type="primary", use_container_width=True)

if extract_clicked and target_url:
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    with st.spinner(f"Connecting via {impersonate_choice} TLS session..."):
        try:
            status_code, html_content, headers = asyncio.run(
                fetch_with_curl_cffi(target_url, impersonate_profile=impersonate_choice)
            )

            if status_code == 200:
                soup = BeautifulSoup(html_content, "html.parser")
                page_title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled Page"
                markdown_result = clean_html_to_markdown(html_content)
                discovered_links = extract_links(html_content, target_url)

                st.success(f"✅ Successfully extracted `{target_url}` (Status: 200 OK)")

                # Top Metrics
                metric_col1, metric_col2, metric_col3 = st.columns(3)
                with metric_col1:
                    st.metric("Page Title", page_title[:35] + ("..." if len(page_title) > 35 else ""))
                with metric_col2:
                    st.metric("Discovered Links", len(discovered_links))
                with metric_col3:
                    st.metric("Content Length (chars)", len(markdown_result))

                st.markdown("---")

                # Result Tabs
                tab1, tab2, tab3, tab4 = st.tabs([
                    "📝 Clean Markdown", 
                    "📊 Structured JSON", 
                    "🔗 Internal Links", 
                    "🌐 Raw HTML"
                ])

                with tab1:
                    st.download_button(
                        label="📥 Download Markdown (.md)",
                        data=markdown_result,
                        file_name="extracted_content.md",
                        mime="text/markdown"
                    )
                    st.markdown(markdown_result)

                with tab2:
                    structured_data = {
                        "url": target_url,
                        "title": page_title,
                        "status_code": status_code,
                        "links_count": len(discovered_links),
                        "content_markdown": markdown_result,
                        "links": discovered_links[:50]
                    }
                    json_str = json.dumps(structured_data, indent=2, ensure_ascii=False)
                    st.download_button(
                        label="📥 Download JSON (.json)",
                        data=json_str,
                        file_name="extracted_data.json",
                        mime="application/json"
                    )
                    st.json(structured_data)

                with tab3:
                    if discovered_links:
                        st.write(f"Found **{len(discovered_links)}** internal links on this page:")
                        st.dataframe(discovered_links, column_config={"value": "Discovered URL"}, use_container_width=True)
                    else:
                        st.info("No internal links found on this page.")

                with tab4:
                    with st.expander("View Raw HTML Source"):
                        st.code(html_content[:10000] + ("\n... [truncated]" if len(html_content) > 10000 else ""), language="html")

            elif status_code == 403 or status_code == 503:
                st.error(f"⚠️ Access blocked by target server (HTTP {status_code}). Try switching the TLS profile in the sidebar.")
            else:
                st.warning(f"Target responded with HTTP {status_code}")

        except Exception as e:
            st.error(f"❌ Extraction error: {str(e)}")

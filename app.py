import asyncio
import json
import re
from urllib.parse import urljoin, urlparse
import streamlit as st
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION
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
    .extracted-box {
        background-color: rgba(128, 128, 128, 0.04);
        border: 1px solid rgba(128, 128, 128, 0.2);
        border-radius: 8px;
        padding: 24px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        line-height: 1.6;
    }
    </style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# EXACT STRUCTURED PARSER
# -----------------------------------------------------------------------------
def extract_exact_content_flow(html_content: str, base_url: str) -> dict:
    """
    Parses the exact editorial and story flow of the website:
    - Separates Eyebrows/Tags (e.g. 'Cybersecurity', 'HPC', 'Agentic AI') from Headlines
    - Preserves exact link texts ('Learn More', 'Read Announcement', 'Register Now')
    - Organizes content under proper Section Headings
    - Eliminates language popups and mega-menu link dumps
    """
    soup = BeautifulSoup(html_content, "html.parser")
    base_domain = urlparse(base_url).netloc
    page_title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"

    # Remove non-content elements
    for el in soup(["script", "style", "noscript", "svg", "iframe", "nav"]):
        el.decompose()

    # Find the main container
    main_container = soup.find("main") or soup.find("article") or soup.find("div", {"id": "page-content"}) or soup.find("body") or soup

    extracted_sections = []
    current_section = {"title": "Top Stories & Announcements", "intro": "", "items": []}
    seen_texts = set()

    # Iterate through content nodes
    for el in main_container.find_all(["h2", "h3", "div", "section", "article"]):
        # Check if this is a major Section Heading (e.g. Artificial Intelligence, Automotive, etc.)
        if el.name == "h2":
            sec_title = el.get_text(strip=True)
            if sec_title and len(sec_title) > 2 and sec_title.lower() not in ["main menu", "quick links", "popular links", "skip to main content"]:
                if current_section["items"] or current_section["intro"]:
                    extracted_sections.append(current_section)
                current_section = {"title": sec_title, "intro": "", "items": []}
            continue

        # Look for cards / story modules inside sections
        # Must have an internal headline (h3, h4, strong) or anchor link
        item_heading = el.find(["h3", "h4", "h5", "strong"])
        if not item_heading:
            continue

        headline = item_heading.get_text(strip=True)
        if not headline or len(headline) < 6 or headline in seen_texts:
            continue

        # Ignore navigation boilerplate
        if any(ign in headline.lower() for ign in ["quick links", "main menu", "company information", "sign in", "skip to"]):
            continue

        seen_texts.add(headline)

        # 1. Extract Tag / Eyebrow (e.g. 'Cybersecurity', 'HPC', 'Agentic AI')
        eyebrow = ""
        eyebrow_el = el.find(class_=lambda c: c and any(k in c.lower() for k in ["kicker", "category", "tag", "eyebrow", "topic", "badge"]))
        if eyebrow_el:
            eyebrow = eyebrow_el.get_text(strip=True)
            if eyebrow == headline:
                eyebrow = ""

        # 2. Extract Description / Paragraph
        description = ""
        p_tag = el.find("p")
        if p_tag:
            desc_text = p_tag.get_text(strip=True)
            if desc_text != headline:
                description = desc_text
        
        # 3. Extract Action Link with exact label ('Learn More', 'Read Announcement', etc.)
        action_link = None
        for a in el.find_all("a", href=True):
            a_text = a.get_text(strip=True)
            href = urljoin(base_url, a["href"])
            if a_text and len(a_text) < 40 and not a_text.startswith(("#", "javascript:")):
                action_link = {"text": a_text, "url": href}
                break

        current_section["items"].append({
            "eyebrow": eyebrow,
            "headline": headline,
            "description": description,
            "action_link": action_link
        })

    if current_section["items"] or current_section["intro"]:
        extracted_sections.append(current_section)

    # Build Exact Formatted Text Stream
    formatted_lines = []
    formatted_lines.append(f"# {page_title}\n")

    for sec in extracted_sections:
        if sec["title"]:
            formatted_lines.append(f"\n## {sec['title']}\n")
        if sec["intro"]:
            formatted_lines.append(f"{sec['intro']}\n")

        for item in sec["items"]:
            if item["eyebrow"]:
                formatted_lines.append(f"**{item['eyebrow']}**")
            formatted_lines.append(f"### {item['headline']}")
            if item["description"]:
                formatted_lines.append(f"{item['description']}")
            if item["action_link"]:
                formatted_lines.append(f"[{item['action_link']['text']}]({item['action_link']['url']})\n")
            else:
                formatted_lines.append("")

    full_text_output = "\n".join(formatted_lines).strip()

    # Discovered Links
    all_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            full_url = urljoin(base_url, href)
            if urlparse(full_url).netloc == base_domain:
                all_links.add(full_url)

    return {
        "title": page_title,
        "url": base_url,
        "formatted_text": full_text_output,
        "sections": extracted_sections,
        "links": sorted(list(all_links))
    }


# -----------------------------------------------------------------------------
# HIGH-SPEED ASYNC ENGINE
# -----------------------------------------------------------------------------
async def fetch_and_extract(url: str) -> dict:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1"
    }
    try:
        async with AsyncSession(headers=headers) as session:
            resp = await session.get(url, impersonate="chrome120", timeout=20)
            if resp.status_code == 200:
                data = extract_exact_content_flow(resp.text, url)
                data["status"] = "SUCCESS"
                return data
            else:
                return {"status": "FAILED", "error": f"Server responded with HTTP {resp.status_code}"}
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


# -----------------------------------------------------------------------------
# MAIN UI
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Web Data Extractor</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Extracts exact content flow with clean headings, tag badges, summaries, and direct links.</div>', unsafe_allow_html=True)

col1, col2 = st.columns([5, 1])
with col1:
    target_url = st.text_input(
        "Target URL",
        value="https://www.nvidia.com/en-in/",
        placeholder="https://example.com",
        label_visibility="collapsed"
    )
with col2:
    extract_btn = st.button("🚀 Extract Data", type="primary", use_container_width=True)

if extract_btn and target_url:
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    with st.spinner("Extracting exact website data..."):
        result = asyncio.run(fetch_and_extract(target_url))

    st.markdown("---")

    if result.get("status") == "SUCCESS":
        st.markdown(f"## {result['title']}")
        st.caption(f"🔗 Source: [{result['url']}]({result['url']})")

        # Metrics
        m1, m2, m3 = st.columns(3)
        with m1:
            total_items = sum(len(s.get("items", [])) for s in result.get("sections", []))
            st.metric("Total Stories / Articles", total_items)
        with m2:
            st.metric("Pillar Sections", len(result.get("sections", [])))
        with m3:
            st.metric("Discovered Links", len(result.get("links", [])))

        st.markdown("---")

        tab_exact, tab_json, tab_links = st.tabs([
            "📄 Exact Formatted Content",
            "📊 Structured JSON",
            "🔗 Discovered Links"
        ])

        with tab_exact:
            st.markdown(result["formatted_text"])

        with tab_json:
            st.json(result)

        with tab_links:
            st.write(f"Found **{len(result['links'])}** internal domain links:")
            for l in result["links"][:50]:
                st.markdown(f"- [{l}]({l})")
    else:
        st.error(f"❌ Extraction failed: {result.get('error', 'Unknown error')}")

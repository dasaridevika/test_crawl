import asyncio
import json
import re
import time
from urllib.parse import urljoin, urlparse
import streamlit as st
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from curl_cffi.requests import AsyncSession

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION
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
# HIGH-FIDELITY LINEAR DOM-TO-MARKDOWN CONVERTER
# -----------------------------------------------------------------------------
def is_noise_or_navigation(tag: Tag) -> bool:
    """Detects purely mechanical UI noise (language popups, skip-to-content, empty carousels)."""
    # Skip navigation, header, footer menus, and utility popups
    if tag.name in ["nav", "header", "footer", "dialog"]:
        return True
    
    # Check class and id attributes
    class_id_str = f"{tag.get('class', '')} {tag.get('id', '')} {tag.get('role', '')}".lower()
    noise_patterns = [
        "cookie", "modal", "language-selector", "region-selector", "country-selector",
        "skip-to-content", "banner-alert", "megamenu", "flyout-menu", "search-modal"
    ]
    if any(p in class_id_str for p in noise_patterns):
        return True

    return False


def dom_to_clean_markdown(root: Tag, base_url: str) -> str:
    """
    Recursively and linearly converts visual DOM elements into formatted Markdown.
    - Preserves exact headline text (H1-H6)
    - Preserves full paragraph text (P, DIV with text)
    - Preserves bullet points (LI)
    - Preserves contextual links with exact anchor text
    - Strips duplicated carousel indicators ('Previous', 'Next', 'Short Description')
    """
    lines = []
    seen_exact_blocks = set()

    # Decompose script, style, comments, and hidden elements
    for el in root(["script", "style", "noscript", "svg", "iframe"]):
        el.decompose()

    for comment in root.find_all(text=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Decompose language/region picker popups
    for tag in root.find_all(["div", "section", "nav"]):
        text_sample = tag.get_text()
        if "Argentina" in text_sample and "Australia" in text_sample and "Belgique" in text_sample:
            tag.decompose()

    # Process all content-bearing tags in document order
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "a", "blockquote", "table"]):
        # Skip if parent was already discarded
        if not el.parent:
            continue

        # Skip navigation containers
        parent_noise = False
        for p in el.parents:
            if is_noise_or_navigation(p):
                parent_noise = True
                break
        if parent_noise:
            continue

        # Extract text
        text = el.get_text(separator=" ", strip=True)
        if not text:
            continue

        # Filter mechanical carousel noise
        if text in ["Previous", "Next", "Short Description"] or re.match(r"^([A-Z0-9\s]{3,30}\s\d+)$", text):
            continue

        # Prevent duplicate identical consecutive blocks
        if text in seen_exact_blocks:
            continue

        tag_name = el.name

        # 1. Headings
        if tag_name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
            level = int(tag_name[1])
            lines.append(f"\n{'#' * level} {text}\n")
            seen_exact_blocks.add(text)

        # 2. Blockquotes
        elif tag_name == "blockquote":
            lines.append(f"> {text}\n")
            seen_exact_blocks.add(text)

        # 3. List items
        elif tag_name == "li":
            # Check if this LI is inside an actual content list, not a menu
            if len(text) > 3:
                lines.append(f"- {text}")
                seen_exact_blocks.add(text)

        # 4. Paragraphs and Content Divs
        elif tag_name == "p":
            # If paragraph contains links, convert them inline
            p_content = text
            for a in el.find_all("a", href=True):
                a_text = a.get_text(strip=True)
                href = urljoin(base_url, a["href"])
                if a_text and href and not href.startswith(("#", "javascript:")):
                    p_content = p_content.replace(a_text, f"[{a_text}]({href})", 1)
            lines.append(f"{p_content}\n")
            seen_exact_blocks.add(text)

        # 5. Standalone Call-to-Action Links (e.g. 'Learn More', 'Register Now')
        elif tag_name == "a" and el.parent.name not in ["p", "li", "h1", "h2", "h3", "h4", "h5", "h6"]:
            href = urljoin(base_url, el.get("href", ""))
            if text and href and not href.startswith(("#", "javascript:")) and len(text) < 80:
                lines.append(f"[{text}]({href})\n")
                seen_exact_blocks.add(text)

    # Format into clean readable Markdown
    clean_lines = []
    consecutive_empty = 0
    for l in lines:
        l_str = l.strip()
        if not l_str:
            consecutive_empty += 1
            if consecutive_empty <= 1:
                clean_lines.append("")
        else:
            consecutive_empty = 0
            clean_lines.append(l_str)

    return "\n".join(clean_lines).strip()


def extract_website_data(html_content: str, base_url: str) -> dict:
    """Extracts exact page content, metadata, schemas, and outlinks."""
    soup = BeautifulSoup(html_content, "html.parser")
    base_domain = urlparse(base_url).netloc
    
    page_title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"

    # Meta Description
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = desc_tag.get("content", "").strip() if desc_tag else ""

    # Extract exact clean Markdown
    clean_markdown = dom_to_clean_markdown(soup, base_url)

    # Extract all discovered internal domain links
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            full_url = urljoin(base_url, href)
            if urlparse(full_url).netloc == base_domain:
                links.add(full_url)

    # Extract JSON-LD Schemas
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
        "url": base_url,
        "markdown": clean_markdown,
        "content_length": len(clean_markdown),
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
                data = extract_website_data(resp.text, url)
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
st.markdown('<div class="sub-header">Extracts exact readable content, headings, paragraphs, and links with zero noise.</div>', unsafe_allow_html=True)

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

    with st.spinner("Extracting exact website content..."):
        result = asyncio.run(fetch_page(target_url))

    st.markdown("---")

    if result.get("status") == "SUCCESS":
        st.markdown(f"## {result['title']}")
        if result.get("description"):
            st.caption(f"**Summary:** {result['description']}")

        # Metrics Bar
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("Total Extracted Length", f"{result['content_length']:,} chars")
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

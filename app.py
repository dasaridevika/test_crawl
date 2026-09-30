import asyncio
import json
from urllib.parse import urljoin, urlparse
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
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
    .main-header { font-size: 2.2rem; font-weight: 700; margin-bottom: 0.2rem; }
    .sub-header { color: #888; font-size: 0.95rem; margin-bottom: 1.5rem; }
    .content-card {
        background-color: rgba(128, 128, 128, 0.05);
        border: 1px solid rgba(128, 128, 128, 0.2);
        border-radius: 10px;
        padding: 16px;
        margin-bottom: 12px;
    }
    .badge {
        display: inline-block;
        padding: 2px 8px;
        font-size: 0.75rem;
        font-weight: 600;
        border-radius: 9999px;
        background-color: rgba(99, 102, 241, 0.15);
        color: #818cf8;
        margin-bottom: 6px;
    }
    </style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# INTELLIGENT CONTENT & CARD EXTRACTOR
# -----------------------------------------------------------------------------
def extract_exact_website_data(html_content: str, base_url: str) -> dict:
    """
    Intelligently extracts the EXACT visible content (Hero features, news cards,
    articles, and announcements) while filtering out mega-menu clutter and language lists.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    base_domain = urlparse(base_url).netloc

    page_title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"
    
    # 1. Clean invisible code & tracking elements
    for el in soup(["script", "style", "noscript", "svg", "iframe"]):
        el.decompose()

    # 2. Extract Hero & Featured Stories / Cards (NVIDIA & Modern Web Cards)
    extracted_cards = []
    seen_titles = set()

    # Look for content cards, articles, and callouts
    for card in soup.find_all(["article", "section", "div"]):
        # Identify heading in this container
        h_tag = card.find(["h2", "h3", "h4", "h5"])
        if not h_tag:
            continue
            
        title_text = h_tag.get_text(strip=True)
        if not title_text or len(title_text) < 5 or title_text in seen_titles:
            continue
            
        # Ignore boilerplate headings (e.g. navigation / regional menus)
        if title_text.lower() in ["main menu", "quick links", "popular links", "sign in", "company information"]:
            continue

        # Extract Category / Tag / Kicker
        category = ""
        kicker_el = card.find(class_=lambda c: c and any(k in c.lower() for k in ["kicker", "category", "tag", "eyebrow", "topic"]))
        if kicker_el:
            category = kicker_el.get_text(strip=True)

        # Extract Description / Paragraph
        desc_text = ""
        p_tag = card.find("p")
        if p_tag:
            desc_text = p_tag.get_text(strip=True)
        else:
            # Fallback to direct text excluding the title
            all_text = [t for t in card.stripped_strings if t != title_text and t != category]
            if all_text:
                desc_text = " ".join(all_text[:3])

        # Extract Link
        link_href = ""
        a_tag = card.find("a", href=True)
        if a_tag:
            link_href = urljoin(base_url, a_tag["href"])

        # Check if card is substantial
        if desc_text or link_href:
            seen_titles.add(title_text)
            extracted_cards.append({
                "title": title_text,
                "category": category or "Featured",
                "description": desc_text,
                "link": link_href
            })

    # 3. Clean Main Body Markdown (Excluding regional popups and massive menus)
    main_soup = BeautifulSoup(html_content, "html.parser")
    for el in main_soup(["script", "style", "noscript", "svg", "iframe", "nav"]):
        el.decompose()

    body = main_soup.find("main") or main_soup.find("article") or main_soup.find("body") or main_soup
    clean_md = md(str(body), heading_style="ATX", strip=["img"], bullets="-")
    
    # Filter redundant blank lines
    md_lines = [l.strip() for l in clean_md.splitlines() if l.strip()]
    formatted_md = "\n\n".join(md_lines)

    # 4. Extract Internal Links
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            full_url = urljoin(base_url, href)
            if urlparse(full_url).netloc == base_domain:
                links.add(full_url)

    return {
        "title": page_title,
        "url": base_url,
        "cards": extracted_cards,
        "markdown": formatted_md,
        "links": sorted(list(links))
    }


# -----------------------------------------------------------------------------
# HIGH-SPEED ASYNC ENGINE
# -----------------------------------------------------------------------------
async def fetch_exact_page(url: str) -> dict:
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
                data = extract_exact_website_data(resp.text, url)
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
st.markdown('<div class="sub-header">Extracts exact articles, story cards, features, and content without mega-menu clutter.</div>', unsafe_allow_html=True)

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
        result = asyncio.run(fetch_exact_page(target_url))

    st.markdown("---")

    if result.get("status") == "SUCCESS":
        st.markdown(f"## {result['title']}")
        st.caption(f"🔗 Source: [{result['url']}]({result['url']})")

        # Metrics
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("Exact Content Cards", len(result.get("cards", [])))
        with m2:
            st.metric("Discovered Links", len(result.get("links", [])))
        with m3:
            st.metric("Status", "✅ 200 OK")

        st.markdown("---")

        tab_cards, tab_md, tab_json, tab_links = st.tabs([
            "📰 Structured Content Cards",
            "📝 Clean Markdown Content",
            "📊 Raw JSON Payload",
            "🔗 Extracted Links"
        ])

        # 1. VISUAL CONTENT CARDS
        with tab_cards:
            st.subheader(f"Found {len(result['cards'])} Core Articles & Features:")
            cards = result.get("cards", [])
            
            if cards:
                # Display in a clean 2-column grid
                c_left, c_right = st.columns(2)
                for idx, card in enumerate(cards):
                    target_col = c_left if idx % 2 == 0 else c_right
                    with target_col:
                        st.markdown(f"""
                        <div class="content-card">
                            <span class="badge">{card['category']}</span>
                            <h4 style="margin: 4px 0 8px 0;">{card['title']}</h4>
                            <p style="color: #bbb; font-size: 0.9rem; margin-bottom: 8px;">{card['description'] or 'No description provided.'}</p>
                            {f'<a href="{card["link"]}" target="_blank" style="color: #6366f1; font-size: 0.85rem; font-weight: 500;">Read More →</a>' if card["link"] else ''}
                        </div>
                        """, unsafe_allow_html=True)
            else:
                st.info("No structured cards found. Check the Markdown tab for full raw text.")

        # 2. CLEAN MARKDOWN TAB
        with tab_md:
            st.markdown(result["markdown"])

        # 3. STRUCTURED JSON TAB
        with tab_json:
            st.json(result)

        # 4. LINKS TAB
        with tab_links:
            st.write(f"Found **{len(result['links'])}** internal links:")
            for l in result["links"][:50]:
                st.markdown(f"- [{l}]({l})")
    else:
        st.error(f"❌ Extraction failed: {result.get('error', 'Unknown error')}")

import asyncio
import json
import re
import time
from urllib.parse import urljoin, urlparse
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from markdownify import markdownify as md

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION & STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Production Web Data Engine",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
    .main-header { font-size: 2.2rem; font-weight: 700; margin-bottom: 0.2rem; }
    .sub-header { color: #888; font-size: 0.95rem; margin-bottom: 1.5rem; }
    .stMarkdown { font-size: 1rem; line-height: 1.7; }
    .highlight-card {
        background-color: rgba(99, 102, 241, 0.08);
        border: 1px solid rgba(99, 102, 241, 0.25);
        border-radius: 8px;
        padding: 14px;
        margin-bottom: 15px;
    }
    </style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# FEATURE-RICH EXTRACTION ENGINE
# -----------------------------------------------------------------------------
def extract_advanced_page_features(html_content: str, base_url: str) -> dict:
    soup = BeautifulSoup(html_content, "html.parser")
    base_domain = urlparse(base_url).netloc
    page_title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"

    # 1. SEO & OpenGraph / Social Metadata
    meta_info = {
        "title": page_title,
        "description": "",
        "keywords": "",
        "author": "",
        "og_title": "",
        "og_description": "",
        "og_image": "",
        "og_type": "",
        "twitter_card": "",
        "canonical": ""
    }
    for m in soup.find_all("meta"):
        name = m.get("name", "").lower()
        prop = m.get("property", "").lower()
        content = m.get("content", "").strip()
        if not content:
            continue

        if name == "description" or prop == "description":
            meta_info["description"] = content
        elif name == "keywords":
            meta_info["keywords"] = content
        elif name == "author":
            meta_info["author"] = content
        elif prop == "og:title":
            meta_info["og_title"] = content
        elif prop == "og:description":
            meta_info["og_description"] = content
        elif prop == "og:image":
            meta_info["og_image"] = urljoin(base_url, content)
        elif prop == "og:type":
            meta_info["og_type"] = content
        elif name == "twitter:card" or prop == "twitter:card":
            meta_info["twitter_card"] = content

    can_link = soup.find("link", rel="canonical")
    if can_link and can_link.get("href"):
        meta_info["canonical"] = urljoin(base_url, can_link["href"])

    # 2. HTML Tables Extraction
    extracted_tables = []
    for idx, tbl in enumerate(soup.find_all("table"), start=1):
        try:
            # Parse HTML table to pandas
            df_list = pd.read_html(str(tbl))
            if df_list and not df_list[0].empty:
                df = df_list[0]
                extracted_tables.append({
                    "id": f"Table #{idx}",
                    "rows": len(df),
                    "columns": len(df.columns),
                    "dataframe": df
                })
        except Exception:
            pass

    # 3. Media & Image Gallery Extraction
    images = []
    seen_img_urls = set()
    for img in soup.find_all("img", src=True):
        src = img.get("src", "").strip()
        if src and not src.startswith("data:image/svg+xml"):
            full_src = urljoin(base_url, src)
            if full_src not in seen_img_urls:
                seen_img_urls.add(full_src)
                alt = img.get("alt", "").strip() or "Image"
                images.append({
                    "src": full_src,
                    "alt": alt,
                    "width": img.get("width", "auto"),
                    "height": img.get("height", "auto")
                })

    # 4. Contact Details & Social Profiles Detection
    emails = set(re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", html_content))
    # Filter common asset extensions misidentified as emails
    clean_emails = [e for e in emails if not e.endswith(('.png', '.jpg', '.webp', '.js', '.svg', '.css'))]

    social_platforms = {
        "github.com": "GitHub",
        "linkedin.com": "LinkedIn",
        "twitter.com": "Twitter/X",
        "x.com": "Twitter/X",
        "youtube.com": "YouTube",
        "facebook.com": "Facebook",
        "instagram.com": "Instagram",
        "discord.gg": "Discord",
        "discord.com": "Discord"
    }
    social_links = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        for domain, name in social_platforms.items():
            if domain in href and name not in social_links:
                social_links[name] = href

    # 5. Code Snippets Extraction
    code_snippets = []
    for pre in soup.find_all(["pre", "code"]):
        code_txt = pre.get_text().strip()
        if len(code_txt) > 20 and "\n" in code_txt:
            lang = pre.get("class", [""])[0] if pre.get("class") else "text"
            if code_txt not in [c["code"] for c in code_snippets]:
                code_snippets.append({"lang": lang, "code": code_txt})

    # 6. Clean Text Content Generation
    clean_soup = BeautifulSoup(html_content, "html.parser")
    for el in clean_soup(["script", "style", "noscript", "svg", "iframe"]):
        el.decompose()

    body = clean_soup.find("body") or clean_soup
    raw_md = md(str(body), heading_style="ATX", strip=["img"], bullets="-")

    # Clean redundant regional/carousel noise
    split_markers = [
        "Cybersecurity Introducing NVIDIA Open Agent Safety Platform",
        "Agentic AI\nNVIDIA and Palantir",
        "Agentic AI\n\nNVIDIA and Palantir",
        "Introducing NVIDIA Open Agent Safety Platform"
    ]
    clean_body = raw_md
    for marker in split_markers:
        if marker in clean_body:
            idx = clean_body.find(marker)
            clean_body = clean_body[idx:]
            break

    clean_body = re.sub(r"\b(Previous|Next)\b", "", clean_body)
    clean_body = re.sub(r"Short Description(\s*\n\s*[^\n]+\d+)+", "", clean_body, flags=re.IGNORECASE)
    clean_body = re.sub(r"(\n\s*[A-Z0-9\s]{3,35}\s\d+\s*\n)+", "\n", clean_body)

    lines = [l.strip() for l in clean_body.splitlines() if l.strip()]
    final_markdown = "\n\n".join(lines).strip()

    # 7. Word Count & Reading Time Estimation
    word_count = len(re.findall(r"\w+", final_markdown))
    est_reading_time = max(1, round(word_count / 220))

    # 8. Internal Domain Links
    internal_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            full_url = urljoin(base_url, href)
            if urlparse(full_url).netloc == base_domain:
                internal_links.add(full_url)

    # 9. JSON-LD Schemas
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
        "metadata": meta_info,
        "word_count": word_count,
        "reading_time_min": est_reading_time,
        "markdown": final_markdown,
        "tables": extracted_tables,
        "images": images,
        "emails": clean_emails,
        "social_links": social_links,
        "code_snippets": code_snippets,
        "links": sorted(list(internal_links)),
        "json_ld": json_ld,
        "content_length": len(final_markdown)
    }


# -----------------------------------------------------------------------------
# HIGH-SPEED ASYNC NETWORK LAYER
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
                data = extract_advanced_page_features(resp.text, url)
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
st.markdown('<div class="main-header">⚡ Web Data Extractor & Intelligence Studio</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Advanced multi-modal web extraction: Full Text, Data Tables, Media Assets, SEO Metadata, Contacts & Code.</div>', unsafe_allow_html=True)

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

    with st.spinner("Extracting multi-modal web data..."):
        result = asyncio.run(fetch_page(target_url))

    st.markdown("---")

    if result.get("status") == "SUCCESS":
        st.markdown(f"## {result['title']}")
        st.caption(f"🔗 Source: [{result['url']}]({result['url']}) | ⏱️ Fetched in {result['fetch_time_sec']}s")

        # Top Metric Cards
        m1, m2, m3, m4, m5 = st.columns(5)
        with m1:
            st.metric("Total Words", f"{result['word_count']:,}")
        with m2:
            st.metric("Reading Time", f"~{result['reading_time_min']} min")
        with m3:
            st.metric("Data Tables", len(result["tables"]))
        with m4:
            st.metric("Images Found", len(result["images"]))
        with m5:
            st.metric("Discovered Links", len(result["links"]))

        st.markdown("---")

        # Feature Tabs
        tab_text, tab_tables, tab_media, tab_seo, tab_contacts, tab_code, tab_json, tab_links = st.tabs([
            "📄 Full Text Content",
            f"📊 Tables ({len(result['tables'])})",
            f"🖼️ Media & Images ({len(result['images'])})",
            "🏷️ SEO & Metadata",
            f"📞 Contacts & Social ({len(result['emails']) + len(result['social_links'])})",
            f"💻 Code Snippets ({len(result['code_snippets'])})",
            "📦 Structured JSON",
            f"🔗 Links ({len(result['links'])})"
        ])

        # 1. FULL TEXT CONTENT
        with tab_text:
            st.markdown(result["markdown"])

        # 2. DATA TABLES
        with tab_tables:
            if result["tables"]:
                st.subheader(f"Extracted {len(result['tables'])} Data Tables:")
                for tbl in result["tables"]:
                    st.markdown(f"#### {tbl['id']} ({tbl['rows']} rows × {tbl['columns']} cols)")
                    st.dataframe(tbl["dataframe"], use_container_width=True)
            else:
                st.info("No HTML data tables (`<table>`) detected on this page.")

        # 3. MEDIA & IMAGES
        with tab_media:
            if result["images"]:
                st.subheader(f"Found {len(result['images'])} Images & Visual Assets:")
                img_cols = st.columns(3)
                for idx, img in enumerate(result["images"][:30]):
                    target_c = img_cols[idx % 3]
                    with target_c:
                        st.image(img["src"], caption=img["alt"][:40] if img["alt"] else "Image", use_container_width=True)
                        st.caption(f"🔗 [View Source]({img['src']})")
            else:
                st.info("No image assets found.")

        # 4. SEO & SOCIAL METADATA
        with tab_seo:
            st.subheader("SEO & Social Graph Metadata:")
            meta = result["metadata"]
            c_meta1, c_meta2 = st.columns(2)
            with c_meta1:
                st.write("**Title:**", meta.get("title", "N/A"))
                st.write("**Description:**", meta.get("description", "N/A"))
                st.write("**Author / Publisher:**", meta.get("author", "N/A"))
                st.write("**Canonical URL:**", meta.get("canonical", "N/A"))
            with c_meta2:
                st.write("**OpenGraph Title:**", meta.get("og_title", "N/A"))
                st.write("**OpenGraph Type:**", meta.get("og_type", "N/A"))
                st.write("**Twitter Card:**", meta.get("twitter_card", "N/A"))
                if meta.get("og_image"):
                    st.write("**Social Share Image:**")
                    st.image(meta["og_image"], width=300)

        # 5. CONTACTS & SOCIAL HANDLES
        with tab_contacts:
            st.subheader("Detected Contacts & Social Handles:")
            c_con1, c_con2 = st.columns(2)
            with c_con1:
                st.write("#### ✉️ Email Addresses")
                if result["emails"]:
                    for e in result["emails"]:
                        st.markdown(f"- `{e}`")
                else:
                    st.caption("No explicit email addresses found in markup.")
            with c_con2:
                st.write("#### 🌐 Social Profiles")
                if result["social_links"]:
                    for platform, s_url in result["social_links"].items():
                        st.markdown(f"- **{platform}:** [{s_url}]({s_url})")
                else:
                    st.caption("No major social profile handles found.")

        # 6. CODE SNIPPETS
        with tab_code:
            if result["code_snippets"]:
                st.subheader(f"Extracted {len(result['code_snippets'])} Code / Command Blocks:")
                for snip in result["code_snippets"]:
                    st.code(snip["code"], language=snip.get("lang", "text"))
            else:
                st.info("No code snippets (`<pre><code>`) found on this page.")

        # 7. STRUCTURED JSON
        with tab_json:
            clean_json_result = {k: v for k, v in result.items() if k != "tables"}
            st.json(clean_json_result)

        # 8. OUTLINKS
        with tab_links:
            st.write(f"Found **{len(result['links'])}** internal domain links:")
            for l in result["links"][:60]:
                st.markdown(f"- [{l}]({l})")

    else:
        st.error(f"❌ Extraction error: {result.get('error', 'Failed to fetch.')}")

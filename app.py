"""
Web Content Extractor & Structured Data Engine
=============================================
Clean, minimal UI powered exclusively by Crawlee for Python + PlaywrightCrawler.
"""

import asyncio
import html
import io
import json
import time
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from crawler import CrawleePlaywrightCrawlerEngine, CrawlConfig, is_safe_url

# Page configuration
st.set_page_config(
    page_title="Web Content Extractor",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom minimal styling
st.markdown("""
    <style>
    .stApp { max-width: 1200px; margin: 0 auto; }
    .main-title { font-size: 1.8rem; font-weight: 700; color: #1e293b; margin-bottom: 0.5rem; }
    .metric-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; text-align: center; }
    .metric-val { font-size: 1.4rem; font-weight: 700; color: #0f172a; }
    .metric-lbl { font-size: 0.75rem; color: #64748b; text-transform: uppercase; font-weight: 600; }
    .badge-success { background: #dcfce7; color: #166534; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem; }
    .badge-error { background: #fee2e2; color: #991b1b; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem; }
    .badge-blocked { background: #fef9c3; color: #854d0e; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem; }
    .badge-skipped { background: #f1f5f9; color: #475569; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem; }
    </style>
""", unsafe_allow_html=True)

# Session state initialization
if "crawl_results" not in st.session_state:
    st.session_state["crawl_results"] = []
if "crawl_stats" not in st.session_state:
    st.session_state["crawl_stats"] = {}
if "is_crawling" not in st.session_state:
    st.session_state["is_crawling"] = False

# App Title
st.markdown('<div class="main-title">🌐 Web Content Extractor</div>', unsafe_allow_html=True)

# Simple URL Input and Extract Button
col_input, col_btn = st.columns([5, 1])
with col_input:
    target_url = st.text_input(
        "Target URL",
        value="https://www.nvidia.com/en-in/",
        placeholder="https://example.com",
        label_visibility="collapsed"
    )

with col_btn:
    extract_clicked = st.button("Extract", type="primary", use_container_width=True, disabled=st.session_state["is_crawling"])

# Advanced Options (Collapsible Expander)
with st.expander("⚙️ Advanced Crawl Settings", expanded=False):
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        max_pages = st.number_input("Max Pages", min_value=1, max_value=500, value=25, step=5)
    with c2:
        max_depth = st.number_input("Max Depth", min_value=0, max_value=5, value=2, step=1)
    with c3:
        concurrency = st.number_input("Concurrency", min_value=1, max_value=6, value=2, step=1)
    with c4:
        request_delay = st.number_input("Delay (s)", min_value=0.0, max_value=3.0, value=0.3, step=0.1)

# Execution Logic
if extract_clicked:
    if not target_url or not target_url.strip():
        st.error("Please enter a valid URL.")
    else:
        target_url = target_url.strip()
        safe, reason = is_safe_url(target_url)
        if not safe:
            st.error(f"Target URL rejected by security policy: {reason}")
        else:
            st.session_state["is_crawling"] = True
            st.session_state["crawl_results"] = []
            st.session_state["crawl_stats"] = {}

            progress_bar = st.progress(0.0)
            status_text = st.empty()
            metrics_container = st.empty()

            config = CrawlConfig(
                seed_url=target_url,
                max_pages=int(max_pages),
                max_depth=int(max_depth),
                concurrency=int(concurrency),
                timeout=30,
                retries=2,
                delay=float(request_delay),
                allow_subdomains=False,
                respect_robots=True,
                discover_sitemaps=True
            )

            crawler = CrawleePlaywrightCrawlerEngine(config)

            def handle_progress(record: Dict[str, Any], stats: Dict[str, Any]):
                attempted = stats.get("attempted", 0)
                frac = min(1.0, attempted / max(1, config.max_pages))
                progress_bar.progress(frac)
                status_text.caption(f"Extracting: {record.get('url', '')} | Pages: {attempted}/{config.max_pages}")

            def run_crawler_sync(engine, cb):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    return loop.run_until_complete(engine.crawl(on_page_crawled=cb))
                finally:
                    try:
                        loop.close()
                    except Exception:
                        pass

            try:
                with st.spinner("Extracting content..."):
                    results = run_crawler_sync(crawler, handle_progress)
                
                st.session_state["crawl_results"] = results
                st.session_state["crawl_stats"] = crawler.stats
                progress_bar.progress(1.0)
                status_text.empty()
            except Exception as e:
                st.error(f"Extraction encountered an issue: {str(e)}")
            finally:
                st.session_state["is_crawling"] = False
                st.rerun()

# Display Results
results: List[Dict[str, Any]] = st.session_state.get("crawl_results", [])
stats: Dict[str, Any] = st.session_state.get("crawl_stats", {})

if results:
    st.markdown("---")
    
    # Summary Metrics Row
    successful_count = sum(1 for r in results if r.get("status") == "SUCCESS")
    total_words = sum(r.get("quality", {}).get("word_count", 0) for r in results)
    total_images = sum(len(r.get("images", [])) for r in results)
    total_links = sum(len(r.get("links", [])) for r in results)
    total_tables = sum(len(r.get("tables", [])) for r in results)
    elapsed = stats.get("elapsed_seconds", 0.0)

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    with m1:
        st.markdown(f'<div class="metric-card"><div class="metric-val">{len(results)}</div><div class="metric-lbl">Pages</div></div>', unsafe_allow_html=True)
    with m2:
        st.markdown(f'<div class="metric-card"><div class="metric-val">{successful_count}</div><div class="metric-lbl">Successful</div></div>', unsafe_allow_html=True)
    with m3:
        st.markdown(f'<div class="metric-card"><div class="metric-val">{total_words:,}</div><div class="metric-lbl">Total Words</div></div>', unsafe_allow_html=True)
    with m4:
        st.markdown(f'<div class="metric-card"><div class="metric-val">{total_images:,}</div><div class="metric-lbl">Images</div></div>', unsafe_allow_html=True)
    with m5:
        st.markdown(f'<div class="metric-card"><div class="metric-val">{total_links:,}</div><div class="metric-lbl">Links</div></div>', unsafe_allow_html=True)
    with m6:
        st.markdown(f'<div class="metric-card"><div class="metric-val">{elapsed}s</div><div class="metric-lbl">Duration</div></div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # Export Section
    exp_c1, exp_c2, exp_c3, exp_c4 = st.columns(4)
    
    # JSONL Export
    jsonl_str = "\n".join(json.dumps(r, ensure_ascii=False) for r in results)
    with exp_c1:
        st.download_button("📥 Export JSONL", data=jsonl_str.encode("utf-8"), file_name="extracted_pages.jsonl", mime="application/x-jsonlines", use_container_width=True)

    # JSON Export
    json_str = json.dumps(results, indent=2, ensure_ascii=False)
    with exp_c2:
        st.download_button("📥 Export JSON", data=json_str.encode("utf-8"), file_name="extracted_pages.json", mime="application/json", use_container_width=True)

    # CSV Export
    csv_rows = []
    for r in results:
        csv_rows.append({
            "url": r.get("url"),
            "final_url": r.get("final_url"),
            "status": r.get("status"),
            "http_status": r.get("http_status"),
            "title": r.get("title"),
            "category": r.get("category"),
            "word_count": r.get("quality", {}).get("word_count", 0),
            "images_count": len(r.get("images", [])),
            "links_count": len(r.get("links", [])),
            "tables_count": len(r.get("tables", [])),
            "meta_description": r.get("meta_description", ""),
            "author": r.get("author", "N/A"),
            "published_date": r.get("published_date", "N/A"),
            "depth": r.get("depth", 0),
            "fetch_time_ms": r.get("fetch_time_ms", 0.0)
        })
    df_csv = pd.DataFrame(csv_rows)
    with exp_c3:
        st.download_button("📥 Export CSV", data=df_csv.to_csv(index=False).encode("utf-8"), file_name="extracted_pages.csv", mime="text/csv", use_container_width=True)

    # Excel Export
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        df_csv.to_excel(writer, index=False, sheet_name="Pages")
    excel_data = excel_buffer.getvalue()
    with exp_c4:
        st.download_button("📥 Export Excel", data=excel_data, file_name="extracted_pages.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

    st.markdown("---")

    # Page Selector & Content Inspector
    st.markdown("### 📄 Extracted Pages")
    
    page_options = [
        f"#{i+1} [{r.get('status')}] {r.get('title', 'Untitled')} ({r.get('quality', {}).get('word_count', 0):,} words) — {r.get('url')}"
        for i, r in enumerate(results)
    ]
    
    selected_idx = st.selectbox(
        "Select page to inspect:",
        options=range(len(page_options)),
        format_func=lambda i: page_options[i]
    )

    if selected_idx is not None and 0 <= selected_idx < len(results):
        page = results[selected_idx]

        st.markdown(f"**URL:** [{page.get('url')}]({page.get('url')}) | **Status:** `{page.get('status')}` (HTTP {page.get('http_status')}) | **Category:** `{page.get('category')}`")

        # Multi-representation Tabs
        tab_rendered, tab_md, tab_text, tab_rhtml, tab_chtml, tab_img, tab_links, tab_tables, tab_meta, tab_json = st.tabs([
            "📄 Formatted Content",
            "📝 Markdown",
            "🔤 Plain Text",
            "🌐 Rendered HTML",
            "🧹 Cleaned HTML",
            "🖼️ Images",
            "🔗 Links",
            "📊 Tables & Cards",
            "🏷️ Metadata",
            "⚙️ JSON"
        ])

        with tab_rendered:
            st.markdown(f"## {page.get('title', 'Untitled')}")
            if page.get("meta_description"):
                st.info(page.get("meta_description"))
            st.markdown(page.get("markdown", "*No markdown content available.*"))

        with tab_md:
            st.code(page.get("markdown", ""), language="markdown")

        with tab_text:
            st.text_area("Plain Text", value=page.get("plain_text", ""), height=450)

        with tab_rhtml:
            st.code(page.get("rendered_html", ""), language="html")

        with tab_chtml:
            st.code(page.get("cleaned_html", ""), language="html")

        with tab_img:
            imgs = page.get("images", [])
            st.markdown(f"**Found {len(imgs)} images**")
            if imgs:
                img_df = pd.DataFrame(imgs)
                st.dataframe(img_df[["url", "alt", "width", "height", "source_attribute", "is_tracking_pixel"]], use_container_width=True)

        with tab_links:
            links = page.get("links", [])
            st.markdown(f"**Found {len(links)} links**")
            if links:
                links_df = pd.DataFrame(links)
                st.dataframe(links_df[["href", "text", "category", "context"]], use_container_width=True)

        with tab_tables:
            tables = page.get("tables", [])
            cards = page.get("cards", [])
            st.markdown(f"**Tables ({len(tables)}) & Cards ({len(cards)})**")
            for t in tables:
                st.markdown(f"**{t.get('id', 'Table')}** {('- ' + t.get('caption')) if t.get('caption') else ''}")
                if t.get("headers") and t.get("rows"):
                    st.dataframe(pd.DataFrame(t["rows"], columns=t["headers"]), use_container_width=True)
                elif t.get("rows"):
                    st.dataframe(pd.DataFrame(t["rows"]), use_container_width=True)
            for c in cards:
                st.markdown(f"- **{c.get('title', 'Card')}**: {c.get('text', '')}")

        with tab_meta:
            st.json({
                "title": page.get("title"),
                "category": page.get("category"),
                "meta_description": page.get("meta_description"),
                "author": page.get("author"),
                "published_date": page.get("published_date"),
                "canonical_url": page.get("canonical_url"),
                "json_ld": page.get("json_ld", []),
                "quality": page.get("quality", {}),
                "warnings": page.get("warnings", []),
                "error": page.get("error")
            })

        with tab_json:
            st.json(page)

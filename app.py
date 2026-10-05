"""
Layer 5: Web Content Extractor & Dataset Engine
==============================================
Ultra-clean, minimal Streamlit UI powered exclusively by Crawlee for Python + PlaywrightCrawler.
"""

import asyncio
import io
import json
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from crawler import CrawlConfig, CrawleeWebCrawler, is_safe_url

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION & MINIMAL STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Web Content Extractor",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
    .stApp { max-width: 1200px; margin: 0 auto; }
    .main-title { font-size: 1.8rem; font-weight: 700; color: #1e293b; margin-bottom: 0.5rem; }
    .metric-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; text-align: center; }
    .metric-val { font-size: 1.4rem; font-weight: 700; color: #0f172a; }
    .metric-lbl { font-size: 0.75rem; color: #64748b; text-transform: uppercase; font-weight: 600; }
    </style>
""", unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# SESSION STATE INITIALIZATION
# -----------------------------------------------------------------------------
if "crawl_results" not in st.session_state:
    st.session_state["crawl_results"] = []
if "crawl_stats" not in st.session_state:
    st.session_state["crawl_stats"] = {}
if "is_crawling" not in st.session_state:
    st.session_state["is_crawling"] = False
if "crawl_error" not in st.session_state:
    st.session_state["crawl_error"] = None

# -----------------------------------------------------------------------------
# TOP HEADER & SIMPLE INPUT
# -----------------------------------------------------------------------------
st.markdown('<div class="main-title">🌐 Web Content Extractor</div>', unsafe_allow_html=True)

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
    c_mode, c1, c2, c3, c4 = st.columns([2, 1, 1, 1, 1])
    with c_mode:
        crawl_mode_label = st.selectbox(
            "Crawl Speed & Mode",
            options=["🌐 Full Headless Browser (Playwright)", "⚡ Turbo (Adaptive Hybrid)", "🏎️ Fast HTTP Only"],
            index=0,
            help="Full Headless Browser uses Playwright Chromium with media/tracker blocking for 100% reliable JS extraction."
        )
        mode_mapping = {
            "🌐 Full Headless Browser (Playwright)": "browser",
            "⚡ Turbo (Adaptive Hybrid)": "turbo",
            "🏎️ Fast HTTP Only": "http"
        }
        selected_mode = mode_mapping.get(crawl_mode_label, "browser")
    with c1:
        max_pages = st.number_input("Max Pages", min_value=1, max_value=200, value=10, step=5)
    with c2:
        max_depth = st.number_input("Max Depth", min_value=0, max_value=5, value=2, step=1)
    with c3:
        concurrency = st.number_input("Concurrency", min_value=1, max_value=8, value=3, step=1)
    with c4:
        request_delay = st.number_input("Delay (s)", min_value=0.0, max_value=3.0, value=0.1, step=0.1)

# -----------------------------------------------------------------------------
# CRAWL EXECUTION
# -----------------------------------------------------------------------------
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
            st.session_state["crawl_error"] = None

            progress_bar = st.progress(0.0)
            status_text = st.empty()

            config = CrawlConfig(
                seed_url=target_url,
                max_pages=int(max_pages),
                max_depth=int(max_depth),
                concurrency=int(concurrency),
                delay=float(request_delay),
                crawl_mode=selected_mode
            )

            crawler = CrawleeWebCrawler(config)

            def handle_progress(record: Dict[str, Any], stats: Dict[str, Any]):
                attempted = stats.get("attempted", 0)
                frac = min(1.0, attempted / max(1, config.max_pages))
                progress_bar.progress(frac)
                status_text.caption(f"Extracting: {record.get('url', '')} | Pages: {attempted}/{config.max_pages}")

            def run_sync():
                import sys
                if sys.platform == "win32":
                    try:
                        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                    except Exception:
                        pass
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    return loop.run_until_complete(crawler.crawl(on_page_crawled=handle_progress))
                finally:
                    try:
                        loop.close()
                    except Exception:
                        pass

            try:
                with st.spinner("Extracting complete site content..."):
                    results = run_sync()
                
                st.session_state["crawl_results"] = results
                st.session_state["crawl_stats"] = crawler.stats
                progress_bar.progress(1.0)
                status_text.empty()
            except Exception as e:
                st.session_state["crawl_error"] = str(e)
            finally:
                st.session_state["is_crawling"] = False
                st.rerun()

# -----------------------------------------------------------------------------
# RESULTS DISPLAY
# -----------------------------------------------------------------------------
if st.session_state.get("crawl_error"):
    st.error(f"Extraction Error: {st.session_state['crawl_error']}")

results: List[Dict[str, Any]] = st.session_state.get("crawl_results", [])
stats: Dict[str, Any] = st.session_state.get("crawl_stats", {})

if results:
    st.markdown("---")

    # Summary Metrics Row
    successful_count = sum(1 for r in results if r.get("status") == "SUCCESS")
    total_words = sum(r.get("word_count", 0) for r in results)
    elapsed = stats.get("elapsed_seconds", 0.0)

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.markdown(f'<div class="metric-card"><div class="metric-val">{len(results)}</div><div class="metric-lbl">Total Pages</div></div>', unsafe_allow_html=True)
    with m2:
        st.markdown(f'<div class="metric-card"><div class="metric-val">{successful_count}</div><div class="metric-lbl">Successful</div></div>', unsafe_allow_html=True)
    with m3:
        st.markdown(f'<div class="metric-card"><div class="metric-val">{total_words:,}</div><div class="metric-lbl">Total Words</div></div>', unsafe_allow_html=True)
    with m4:
        st.markdown(f'<div class="metric-card"><div class="metric-val">{elapsed}s</div><div class="metric-lbl">Duration</div></div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # Export Section
    exp_c1, exp_c2, exp_c3, exp_c4 = st.columns(4)
    
    # JSONL Export
    jsonl_str = "\n".join(json.dumps(r, ensure_ascii=False) for r in results)
    with exp_c1:
        st.download_button("📥 Export JSONL", data=jsonl_str.encode("utf-8"), file_name="crawled_dataset.jsonl", mime="application/x-jsonlines", use_container_width=True)

    # JSON Export
    json_str = json.dumps(results, indent=2, ensure_ascii=False)
    with exp_c2:
        st.download_button("📥 Export JSON", data=json_str.encode("utf-8"), file_name="crawled_dataset.json", mime="application/json", use_container_width=True)

    # CSV Export
    csv_rows = []
    for r in results:
        csv_rows.append({
            "url": r.get("url"),
            "final_url": r.get("final_url"),
            "status": r.get("status"),
            "http_status": r.get("http_status"),
            "title": r.get("title"),
            "word_count": r.get("word_count", 0),
            "character_count": r.get("character_count", 0),
            "meta_description": r.get("meta_description", ""),
            "depth": r.get("depth", 0),
            "fetch_time_ms": r.get("fetch_time_ms", 0.0)
        })
    df_csv = pd.DataFrame(csv_rows)
    with exp_c3:
        st.download_button("📥 Export CSV", data=df_csv.to_csv(index=False).encode("utf-8"), file_name="crawled_dataset.csv", mime="text/csv", use_container_width=True)

    # Excel Export
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        df_csv.to_excel(writer, index=False, sheet_name="Pages")
    excel_data = excel_buffer.getvalue()
    with exp_c4:
        st.download_button("📥 Export Excel", data=excel_data, file_name="crawled_dataset.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

    st.markdown("---")

    # Page Selector & Content Inspector
    st.markdown("### 📄 Extracted Page Content")
    
    page_options = [
        f"#{i+1} [{r.get('status')}] {r.get('title', 'Untitled')} ({r.get('word_count', 0):,} words) — {r.get('url')}"
        for i, r in enumerate(results)
    ]
    
    selected_idx = st.selectbox(
        "Select page to inspect:",
        options=range(len(page_options)),
        format_func=lambda i: page_options[i]
    )

    if selected_idx is not None and 0 <= selected_idx < len(results):
        page = results[selected_idx]

        st.markdown(f"**URL:** [{page.get('url')}]({page.get('url')}) | **Status:** `{page.get('status')}` (HTTP {page.get('http_status')}) | **Words:** `{page.get('word_count'):,}`")

        # Multi-representation Tabs
        tab_rendered, tab_md, tab_text, tab_rhtml, tab_json = st.tabs([
            "📄 Formatted Content",
            "📝 Markdown",
            "🔤 Plain Text",
            "🌐 Rendered HTML",
            "⚙️ JSON"
        ])

        with tab_rendered:
            st.markdown(f"## {page.get('title', 'Untitled')}")
            if page.get("error"):
                st.error(f"Error Details: {page.get('error')}")
            if page.get("meta_description"):
                st.info(page.get("meta_description"))
            st.markdown(page.get("markdown", "*No content available.*"))

        with tab_md:
            st.code(page.get("markdown", ""), language="markdown")

        with tab_text:
            st.text_area("Plain Text (100% Un-truncated)", value=page.get("plain_text", ""), height=500)

        with tab_rhtml:
            st.code(page.get("rendered_html", ""), language="html")

        with tab_json:
            st.json(page)

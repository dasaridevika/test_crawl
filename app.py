"""
Enterprise Web Crawler & Structured Dataset Engine
==================================================
Streamlit Frontend powered exclusively by Crawlee for Python + PlaywrightCrawler.
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

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION & STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Enterprise Web Crawler & Data Engine",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
    <style>
    .main-header { font-size: 2.1rem; font-weight: 700; color: #1e293b; margin-bottom: 0.2rem; }
    .sub-header { color: #64748b; font-size: 0.95rem; margin-bottom: 1.2rem; }
    .status-banner { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; padding: 10px 14px; border-radius: 6px; font-weight: 600; margin-bottom: 14px; }
    .metric-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; text-align: center; }
    .metric-val { font-size: 1.5rem; font-weight: 700; color: #0f172a; }
    .metric-lbl { font-size: 0.8rem; color: #64748b; text-transform: uppercase; font-weight: 600; }
    .badge-success { background: #dcfce7; color: #166534; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem; }
    .badge-warning { background: #fef9c3; color: #854d0e; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem; }
    .badge-danger { background: #fee2e2; color: #991b1b; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem; }
    .badge-info { background: #e0f2fe; color: #075985; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem; }
    .section-card { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px 16px; margin-bottom: 12px; }
    .card-item { background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px; margin-bottom: 8px; }
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


# -----------------------------------------------------------------------------
# SIDEBAR / CRAWL SETTINGS
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### ⚙️ Crawl Configuration")

    seed_url_input = st.text_input(
        "Target Seed URL",
        value="https://example.com",
        help="Root URL of authorized domain to crawl"
    )

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        max_pages = st.number_input("Max Pages", min_value=1, max_value=500, value=25, step=5)
    with col_s2:
        max_depth = st.number_input("Max Depth", min_value=0, max_value=10, value=2, step=1)

    col_s3, col_s4 = st.columns(2)
    with col_s3:
        concurrency = st.number_input("Concurrency", min_value=1, max_value=8, value=2, step=1)
    with col_s4:
        request_delay = st.number_input("Delay (s)", min_value=0.0, max_value=5.0, value=0.3, step=0.1)

    timeout = st.slider("Timeout (seconds)", min_value=10, max_value=90, value=30, step=5)

    st.markdown("---")
    st.markdown("### 🛡️ Policies & Discovery")
    respect_robots = st.checkbox("Respect robots.txt", value=True)
    discover_sitemaps = st.checkbox("Discover XML Sitemaps", value=True)
    allow_subdomains = st.checkbox("Allow Subdomains", value=False)

    st.markdown("---")
    st.caption("⚡ Powered exclusively by **Crawlee for Python + PlaywrightCrawler** (Headless Chromium).")


# -----------------------------------------------------------------------------
# MAIN HEADER & ENGINE STATUS
# -----------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Enterprise Web Crawler & Data Engine</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-header">Preserves rendered page content and produces structured HTML, Markdown, text, metadata, media, links, tables, and completeness diagnostics.</div>',
    unsafe_allow_html=True
)

st.markdown('<div class="status-banner">✅ <b>Crawlee Playwright Browser Crawler Active</b> (Chromium + RequestQueue Deduplication + Lossless DOM Preservation)</div>', unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# CRAWL EXECUTION TRIGGER
# -----------------------------------------------------------------------------
col_action1, col_action2 = st.columns([4, 1])
with col_action1:
    st.info(f"Target: **{seed_url_input}** | Configured Limit: **{max_pages} pages** (Max Depth: {max_depth}) | Concurrency: **{concurrency}** | Delay: **{request_delay}s**")

with col_action2:
    start_button = st.button("🚀 Start Crawl", type="primary", use_container_width=True, disabled=st.session_state["is_crawling"])


if start_button:
    # SSRF & URL Validation
    safe, reason = is_safe_url(seed_url_input)
    if not safe:
        st.error(f"❌ Target URL rejected by security policy: {reason}")
    else:
        st.session_state["is_crawling"] = True
        st.session_state["crawl_results"] = []
        st.session_state["crawl_stats"] = {}

        progress_bar = st.progress(0.0)
        status_text = st.empty()
        metrics_container = st.empty()
        log_container = st.empty()

        config = CrawlConfig(
            seed_url=seed_url_input,
            max_pages=int(max_pages),
            max_depth=int(max_depth),
            concurrency=int(concurrency),
            timeout=int(timeout),
            retries=2,
            delay=float(request_delay),
            allow_subdomains=allow_subdomains,
            respect_robots=respect_robots,
            discover_sitemaps=discover_sitemaps
        )

        crawler = CrawleePlaywrightCrawlerEngine(config)
        logs_history: List[str] = []

        def handle_progress(record: Dict[str, Any], stats: Dict[str, Any]):
            attempted = stats.get("attempted", 0)
            frac = min(1.0, attempted / max(1, config.max_pages))
            progress_bar.progress(frac)

            words = record.get("quality", {}).get("word_count", 0)
            imgs = len(record.get("images", []))
            status_text.markdown(f"**Crawling:** `{record.get('url', '')}` | Status: **{record.get('status')}** ({words} words, {imgs} images)")

            with metrics_container.container():
                m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
                m1.metric("Attempted", f"{stats.get('attempted', 0)} / {config.max_pages}")
                m2.metric("Successful", stats.get("successful", 0))
                m3.metric("Incomplete", stats.get("incomplete", 0))
                m4.metric("Blocked", stats.get("blocked", 0))
                m5.metric("In Queue", stats.get("pages_in_frontier", 0))
                m6.metric("Avg Latency", f"{stats.get('avg_latency_ms', 0):.0f} ms")
                m7.metric("Elapsed", f"{stats.get('elapsed_seconds', 0):.1f} s")

            log_line = f"[{record.get('status')}] {record.get('url')} - {record.get('title', 'Untitled')[:40]} ({words} words, {imgs} img)"
            logs_history.append(log_line)
            with log_container.container():
                st.code("\n".join(logs_history[-8:]), language="text")

        try:
            with st.spinner("Executing Crawlee PlaywrightCrawler with browser rendering..."):
                results = asyncio.run(crawler.crawl(on_page_crawled=handle_progress))
                st.session_state["crawl_results"] = results
                st.session_state["crawl_stats"] = crawler.stats
                progress_bar.progress(1.0)
        except Exception as e:
            st.error(f"❌ Crawl failed with error: {str(e)}")
        finally:
            st.session_state["is_crawling"] = False


# -----------------------------------------------------------------------------
# DATASET VIEW & RECORD INSPECTOR
# -----------------------------------------------------------------------------
results = st.session_state.get("crawl_results", [])
stats = st.session_state.get("crawl_stats", {})

if results:
    st.markdown("---")
    st.markdown("### 📊 Extracted Dataset & Diagnostics")

    # Transparent Completion Status Banner
    if stats.get("crawl_completion_status") == "PARTIAL_LIMIT_REACHED":
        st.warning(f"⚠️ **Partial Crawl:** {stats.get('completion_message')}")
    elif stats.get("crawl_completion_status") == "COMPLETED_ALL_DISCOVERED":
        st.success(f"🎉 **Crawl Completed:** {stats.get('completion_message')}")

    # Summary Metrics Row
    c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
    c1.metric("Total Crawled", len(results))
    c2.metric("Successful", stats.get("successful", 0))
    c3.metric("Incomplete", stats.get("incomplete", 0))
    c4.metric("Blocked / Denied", stats.get("blocked", 0))
    c5.metric("Total Words", f"{stats.get('total_words', 0):,}")
    c6.metric("Total Images", f"{stats.get('total_images', 0):,}")
    c7.metric("Total Links", f"{stats.get('total_links', 0):,}")
    c8.metric("Elapsed", f"{stats.get('elapsed_seconds', 0):.1f}s")

    # Export Buttons Row
    st.markdown("#### 📥 Export Full Structured Dataset")
    exp1, exp2, exp3, exp4 = st.columns(4)

    # 1. JSONL Export (Lossless)
    jsonl_buffer = io.StringIO()
    for item in results:
        jsonl_buffer.write(json.dumps(item, ensure_ascii=False) + "\n")
    exp1.download_button(
        "📥 Download JSONL",
        data=jsonl_buffer.getvalue().encode("utf-8"),
        file_name="crawl_dataset.jsonl",
        mime="application/x-ndjson",
        use_container_width=True
    )

    # 2. JSON Export
    json_bytes = json.dumps(results, indent=2, ensure_ascii=False).encode("utf-8")
    exp2.download_button(
        "📥 Download JSON",
        data=json_bytes,
        file_name="crawl_dataset.json",
        mime="application/json",
        use_container_width=True
    )

    # 3. CSV Summary Export
    summary_rows = []
    for r in results:
        summary_rows.append({
            "URL": r.get("url"),
            "Status": r.get("status"),
            "HTTP Status": r.get("http_status"),
            "Title": r.get("title"),
            "Category": r.get("category"),
            "Word Count": r.get("quality", {}).get("word_count", 0),
            "Images Count": len(r.get("images", [])),
            "Links Count": len(r.get("links", [])),
            "Tables Count": len(r.get("tables", [])),
            "Content Bytes": r.get("content_bytes"),
            "Complete": r.get("content_complete"),
            "Warnings": ", ".join(r.get("warnings", [])),
            "Author": r.get("author"),
            "Date": r.get("published_date"),
            "Summary": r.get("meta_description")
        })
    df_summary = pd.DataFrame(summary_rows)
    csv_bytes = df_summary.to_csv(index=False).encode("utf-8")
    exp3.download_button(
        "📥 Download CSV Summary",
        data=csv_bytes,
        file_name="crawl_summary.csv",
        mime="text/csv",
        use_container_width=True
    )

    # 4. Excel Summary Export
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        df_summary.to_excel(writer, index=False, sheet_name="Crawl Summary")
    exp4.download_button(
        "📥 Download Excel (.xlsx)",
        data=excel_buffer.getvalue(),
        file_name="crawl_summary.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )

    # Interactive Search & Filter Controls
    st.markdown("#### 🔍 Filter & Search Records")
    col_f1, col_f2 = st.columns([3, 1])
    with col_f1:
        search_query = st.text_input("Search in titles, URLs, or descriptions...", "")
    with col_f2:
        status_filter = st.selectbox("Filter Status", ["All", "SUCCESS", "INCOMPLETE", "BLOCKED", "ERROR", "SKIPPED"])

    filtered_records = []
    for i, r in enumerate(results):
        r_status = r.get("status", "")
        if status_filter == "INCOMPLETE" and (r.get("content_complete") or r_status != "SUCCESS"):
            continue
        elif status_filter != "All" and status_filter != "INCOMPLETE" and r_status != status_filter:
            continue

        if search_query:
            q = search_query.lower()
            text_match = (
                q in r.get("title", "").lower()
                or q in r.get("url", "").lower()
                or q in r.get("meta_description", "").lower()
            )
            if not text_match:
                continue

        filtered_records.append((i, r))

    st.caption(f"Showing {len(filtered_records)} of {len(results)} records")

    # Table preview
    if filtered_records:
        preview_table_data = []
        for idx, r in filtered_records:
            preview_table_data.append({
                "#": idx + 1,
                "Status": r.get("status"),
                "Title": r.get("title"),
                "Category": r.get("category"),
                "Words": r.get("quality", {}).get("word_count", 0),
                "Images": len(r.get("images", [])),
                "Links": len(r.get("links", [])),
                "Complete": "✓ Yes" if r.get("content_complete") else "⚠️ Incomplete",
                "Warnings": ", ".join(r.get("warnings", [])),
                "URL": r.get("url")
            })
        st.dataframe(pd.DataFrame(preview_table_data), use_container_width=True, hide_index=True)

        # ---------------------------------------------------------------------
        # DETAILED RECORD INSPECTOR
        # ---------------------------------------------------------------------
        st.markdown("---")
        st.markdown("### 🔎 Page Content & Entity Inspector")

        record_options = [f"#{idx+1}: [{r.get('status')}] {r.get('title', 'Untitled')[:55]} ({r.get('url')})" for idx, r in filtered_records]
        selected_option = st.selectbox("Select Record to Inspect:", record_options)
        selected_index = int(selected_option.split(":")[0].replace("#", "")) - 1
        record = results[selected_index]

        # Record Summary Card
        status_badge = {
            "SUCCESS": '<span class="badge-success">SUCCESS</span>',
            "BLOCKED": '<span class="badge-danger">BLOCKED</span>',
            "ERROR": '<span class="badge-danger">ERROR</span>',
            "SKIPPED": '<span class="badge-warning">SKIPPED</span>'
        }.get(record.get("status", ""), '<span class="badge-info">UNKNOWN</span>')

        complete_badge = '<span class="badge-success">COMPLETE</span>' if record.get("content_complete") else '<span class="badge-warning">INCOMPLETE</span>'

        st.markdown(f"""
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin-bottom: 12px;">
                <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 6px;">
                    {status_badge}
                    {complete_badge}
                    <span class="badge-info">{html.escape(record.get('category', 'Web Page'))}</span>
                </div>
                <h3 style="margin: 4px 0 8px 0; color: #0f172a;">{html.escape(record.get('title', 'Untitled Page'))}</h3>
                <p style="color: #64748b; margin-bottom: 8px;">{html.escape(record.get('meta_description', ''))}</p>
                <div style="font-size: 0.85rem; color: #475569; display: flex; flex-wrap: wrap; gap: 16px;">
                    <span><b>URL:</b> <a href="{html.escape(record.get('url', ''))}" target="_blank">{html.escape(record.get('url', ''))}</a></span>
                    <span><b>HTTP Status:</b> {record.get('http_status')}</span>
                    <span><b>Words:</b> {record.get('quality', {}).get('word_count', 0):,}</span>
                    <span><b>Size:</b> {record.get('content_bytes', 0):,} bytes</span>
                    <span><b>Fetch Time:</b> {record.get('fetch_time_ms', 0)} ms</span>
                    <span><b>Author:</b> {html.escape(record.get('author', 'N/A'))}</span>
                    <span><b>Date:</b> {html.escape(record.get('published_date', 'N/A'))}</span>
                </div>
            </div>
        """, unsafe_allow_html=True)

        if record.get("warnings"):
            st.warning(f"⚠️ **Completeness Diagnostics / Warnings:** {', '.join(record.get('warnings', []))}")
        if record.get("error"):
            st.error(f"❌ **Error Detail:** {record.get('error', {}).get('code')}: {record.get('error', {}).get('message')}")

        # Multi-Tab Inspector
        (
            tab_rendered,
            tab_cleaned,
            tab_md,
            tab_text,
            tab_sections,
            tab_imgs,
            tab_links,
            tab_tables,
            tab_meta,
            tab_raw,
            tab_diag,
            tab_json
        ) = st.tabs([
            "📄 Rendered HTML",
            "🧹 Cleaned HTML",
            "📝 Markdown",
            "🔤 Plain Text",
            f"📑 Sections ({len(record.get('sections', []))})",
            f"🖼️ Images ({len(record.get('images', []))})",
            f"🔗 Links ({len(record.get('links', []))})",
            f"📊 Tables ({len(record.get('tables', []))})",
            "🏷️ Metadata & JSON-LD",
            "🌐 Raw HTML",
            "🩺 Diagnostics",
            "📦 Full JSON"
        ])

        with tab_rendered:
            st.caption("Final browser-rendered DOM captured after JavaScript execution (Source of Truth):")
            st.code(record.get("rendered_html", ""), language="html")

        with tab_cleaned:
            st.caption("Cleaned Extracted Content (DOM with navigation, boilerplate, and scripts stripped):")
            st.code(record.get("cleaned_html", ""), language="html")

        with tab_md:
            st.caption("Derived Markdown representation generated from Cleaned HTML:")
            st.markdown(record.get("markdown", ""))
            with st.expander("View Raw Markdown Source"):
                st.code(record.get("markdown", ""), language="markdown")

        with tab_text:
            st.caption("Derived plain-text representation:")
            st.text_area("Plain Text Content", value=record.get("plain_text", ""), height=400)

        with tab_sections:
            st.caption("Structured Sections and Card Grids extracted from page hierarchy:")
            sections = record.get("sections", [])
            cards = record.get("cards", [])

            if cards:
                st.markdown(f"#### 🃏 Extracted Cards ({len(cards)} items)")
                card_cols = st.columns(min(3, len(cards)))
                for idx, c in enumerate(cards):
                    with card_cols[idx % len(card_cols)]:
                        st.markdown(f"""
                            <div class="card-item">
                                <b>{html.escape(c.get('title', 'Card'))}</b><br>
                                <span style="font-size: 0.85rem; color: #475569;">{html.escape(c.get('text', '')[:120])}...</span><br>
                                <a href="{html.escape(c.get('url', '#'))}" target="_blank" style="font-size: 0.8rem;">Open Link ↗</a>
                            </div>
                        """, unsafe_allow_html=True)

            if sections:
                st.markdown(f"#### 📑 Content Sections ({len(sections)} sections)")
                for sec in sections:
                    with st.expander(f"H{sec.get('level', 2)}: {sec.get('heading', 'Section')} ({sec.get('type', 'content')})", expanded=False):
                        st.markdown(sec.get("text", ""))
                        if sec.get("images"):
                            st.caption(f"Images in section: {len(sec['images'])}")
                        if sec.get("links"):
                            st.caption(f"Links in section: {len(sec['links'])}")

        with tab_imgs:
            images = record.get("images", [])
            st.caption(f"Extracted {len(images)} images from img, picture, srcset, and metadata:")
            if images:
                df_imgs = pd.DataFrame(images)
                st.dataframe(df_imgs, use_container_width=True)
                with st.expander("🖼️ Preview Images"):
                    preview_imgs = [im for im in images if not im.get("is_tracking_pixel") and not im.get("is_svg")][:12]
                    for img_obj in preview_imgs:
                        img_url = img_obj.get("url", "")
                        st.caption(f"{img_obj.get('alt') or 'Image'} | {img_obj.get('source_attribute')} | {img_url}")
                        try:
                            st.image(img_url, width=320)
                        except Exception:
                            pass
            else:
                st.info("No images detected on this page.")

        with tab_links:
            links = record.get("links", [])
            st.caption(f"Discovered {len(links)} classified links across the page:")
            if links:
                st.dataframe(pd.DataFrame(links), use_container_width=True)
            else:
                st.info("No outlinks detected on this page.")

        with tab_tables:
            tables = record.get("tables", [])
            if tables:
                for tbl in tables:
                    st.markdown(f"**{tbl.get('id', 'Table')}** ({tbl.get('row_count')} rows × {tbl.get('column_count')} columns) - *{tbl.get('caption', 'No caption')}*")
                    df_t = pd.DataFrame(tbl.get("rows", []), columns=tbl.get("headers", []))
                    st.dataframe(df_t, use_container_width=True)
            else:
                st.info("No HTML tables detected on this page.")

        with tab_meta:
            st.markdown("#### Page Metadata")
            st.json({
                "Title": record.get("title"),
                "Canonical URL": record.get("canonical_url"),
                "Category": record.get("category"),
                "Author": record.get("author"),
                "Published Date": record.get("published_date"),
                "Meta Description": record.get("meta_description"),
                "Headings": record.get("headings", [])
            })
            st.markdown("#### JSON-LD Schemas")
            json_ld_items = record.get("json_ld", [])
            if json_ld_items:
                for j_item in json_ld_items:
                    st.json(j_item)
            else:
                st.info("No JSON-LD structured schema found.")

        with tab_raw:
            st.caption("Initial raw server response HTML (if captured separately):")
            if record.get("raw_html"):
                st.code(record.get("raw_html", ""), language="html")
            else:
                st.info("Browser direct rendering used. See 'Rendered HTML' tab for full DOM snapshot.")

        with tab_diag:
            st.markdown("#### Extraction Diagnostics & Quality Metrics")
            qual = record.get("quality", {})
            st.json(qual)

            dup_blocks = qual.get("duplicate_blocks", [])
            if dup_blocks:
                st.markdown(f"#### 🔁 Duplicate Component Diagnostics ({len(dup_blocks)} detected)")
                for d in dup_blocks:
                    st.markdown(f"- **Fingerprint:** `{d['fingerprint']}` (Repeated {d['occurrences']}x across {', '.join(d['locations'])})")
                    st.caption(f"  *Sample:* {d['text_sample']}")

        with tab_json:
            st.caption("Complete unstructured & structured dictionary representation:")
            st.json(record)

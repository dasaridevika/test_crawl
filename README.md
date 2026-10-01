# Enterprise Web Crawler & Structured Dataset Engine

A production-grade, high-accuracy, scalable web crawler and structured data extraction engine built exclusively on **Crawlee for Python + PlaywrightCrawler**.

Designed for deep crawling, headless Chromium rendering, strict SSRF security, robots.txt compliance, semantic sectioning, multi-source image extraction, and lossless content preservation across raw, rendered, cleaned, markdown, and structured formats.

---

## ⚡ Key Architecture & Features

1. **Single Unified Crawler Engine**:
   - Built on **Crawlee `PlaywrightCrawler`** with Chromium browser pool management.
   - Eliminates fragmented crawler code. Both the CLI (`crawler.py`) and Streamlit UI (`app.py`) share the same underlying crawler core.

2. **JavaScript & Browser Rendering**:
   - Executes dynamic client-side JavaScript, single-page applications (React, Vue, Next.js), and dynamic hydration.
   - Captures the complete post-execution DOM (`rendered_html`) as the source-of-truth representation.

3. **Lossless Content Preservation**:
   - Preserves all data without arbitrary string slicing, link limits, or silent omission.
   - Separates source-of-truth data from derived representations:
     - **Rendered HTML**: Full post-JavaScript DOM snapshot.
     - **Raw HTML**: Initial server response (if available).
     - **Cleaned HTML**: Sanitized DOM without navigation boilerplate, footer, scripts, or cookie banners.
     - **Markdown**: Clean, structured Markdown derived from the cleaned DOM.
     - **Plain Text**: Lossless textual extraction.
     - **Structured Sections & Cards**: Hierarchical content sections and card grids.
     - **Entities**: Classified links, comprehensive images, serializable tables, and JSON-LD schemas.

4. **Comprehensive Image Extraction**:
   - Extracts image candidates from `img[src]`, `img[data-src]`, `img[data-original]`, `img[srcset]`, `<picture><source srcset>`, Open Graph metadata (`og:image`), and inline CSS `background-image`.
   - Preserves and classifies SVGs (`is_svg`) and detects tracking pixels (`is_tracking_pixel`).

5. **Duplicate Component Diagnostics**:
   - Fingerprints repeated text and card blocks using SHA-256.
   - Classifies block occurrences across `main_content`, `carousel`, `mobile`, `modal`, `navigation`, and `footer`.

6. **Deep Crawling & Transparent Limit Reporting**:
   - Request queue deduplication and scheduling powered by Crawlee `RequestQueue`.
   - Discovers seed and deeper URLs from `sitemap.xml`, `sitemap_index.xml`, and `robots.txt` declarations.
   - URL normalization strips tracking parameters (`utm_*`, `fbclid`, `gclid`, etc.) and fragments (`#`).
   - When page limits are reached, reports status as `PARTIAL_LIMIT_REACHED` and explicitly states remaining uncrawled URLs.

7. **Security & SSRF Protection**:
   - Validates all seed URLs, discovered links, and redirect destinations.
   - Blocks private IP ranges (RFC 1918), loopback (`127.0.0.1`, `::1`), link-local (`169.254.0.0/16`), multicast, and cloud metadata endpoints (`169.254.169.254`, `metadata.google.internal`).

8. **Website Policies & Rate Limiting**:
   - Respects `robots.txt` rules and crawl delay directives by default.
   - Bounded concurrency with configurable worker tabs and exponential backoff retries.

---

## 📋 Complete Output Schema

Each crawled URL returns a structured record conforming to the following schema:

```json
{
  "url": "https://example.com/article",
  "final_url": "https://example.com/article/",
  "status": "SUCCESS",
  "http_status": 200,
  "crawl_method": "crawlee_playwright",
  "rendered": true,
  "content_complete": true,
  "raw_html": "",
  "rendered_html": "<!DOCTYPE html><html>...</html>",
  "cleaned_html": "<article><h1>Title</h1><p>Content...</p></article>",
  "markdown": "# Title\n\nContent...",
  "plain_text": "Title\n\nContent...",
  "title": "Article Title",
  "category": "Blog / Article",
  "meta_description": "Article summary...",
  "author": "Jane Doe",
  "published_date": "2026-09-30",
  "canonical_url": "https://example.com/article",
  "headings": [
    {"level": 1, "text": "Article Title"},
    {"level": 2, "text": "Section 1"}
  ],
  "sections": [
    {
      "heading": "Section 1",
      "level": 2,
      "type": "content_section",
      "text": "Section text...",
      "html": "<div>...</div>",
      "links": ["https://example.com/docs"],
      "images": ["https://example.com/chart.png"],
      "cards": []
    }
  ],
  "cards": [
    {
      "title": "Feature Card",
      "text": "Description...",
      "url": "https://example.com/feature",
      "image": "https://example.com/thumb.png",
      "context": "main_content"
    }
  ],
  "images": [
    {
      "url": "https://example.com/hero.jpg",
      "alt": "Hero Banner",
      "title": "Banner",
      "width": 1920,
      "height": 1080,
      "source_attribute": "srcset",
      "is_svg": false,
      "is_tracking_pixel": false,
      "context": "hero"
    }
  ],
  "links": [
    {
      "href": "https://example.com/docs",
      "text": "Documentation",
      "title": "Docs",
      "category": "internal",
      "context": "main_content"
    }
  ],
  "tables": [
    {
      "id": "Table #1",
      "caption": "Performance",
      "headers": ["Metric", "Value"],
      "rows": [["Throughput", "10,000 req/s"]],
      "row_count": 1,
      "column_count": 2,
      "source_html": "<table>...</table>"
    }
  ],
  "json_ld": [
    {"@context": "https://schema.org", "@type": "Article"}
  ],
  "quality": {
    "word_count": 450,
    "character_count": 3120,
    "headings_count": 2,
    "sections_count": 1,
    "cards_count": 1,
    "images_count": 1,
    "links_count": 1,
    "tables_count": 1,
    "duplicate_blocks_count": 0,
    "duplicate_blocks": []
  },
  "warnings": [],
  "error": null,
  "depth": 0,
  "fetch_time_ms": 1250.45,
  "extraction_time_ms": 14.20,
  "content_bytes": 35240
}
```

---

## 🚀 Installation & Setup

### 1. Install Python Dependencies
```bash
pip install -r requirements.txt
```

### 2. Install Playwright Browsers
Install the required Chromium browser for Playwright:
```bash
playwright install chromium
```

---

## 💻 CLI Usage

Run a deep crawl from the command line:

```bash
# Basic crawl with default settings (100 pages, depth 3)
python crawler.py https://example.com

# Custom deep crawl configuration
python crawler.py https://example.com \
  --max-pages 50 \
  --max-depth 2 \
  --concurrency 4 \
  --delay 0.5 \
  --timeout 30 \
  --output dataset.jsonl
```

### CLI Options

| Argument | Description | Default |
|---|---|---|
| `url` | Seed URL to begin crawling (required) | - |
| `--max-pages` | Maximum number of pages to crawl | `100` |
| `--max-depth` | Maximum link depth from seed | `3` |
| `--concurrency` | Browser concurrency / worker tabs | `4` |
| `--timeout` | Page render timeout in seconds | `30` |
| `--retries` | Retry attempts on transient errors | `2` |
| `--delay` | Politeness delay between requests (seconds) | `0.5` |
| `--max-response-bytes` | Maximum response byte limit per page | `10485760` (10 MB) |
| `--output` | Output file path (`.jsonl` or `.json`) | `output.jsonl` |
| `--allow-subdomains` | Allow crawling subdomains of seed | `False` |
| `--ignore-robots` | Bypass robots.txt check | `False` |
| `--no-sitemaps` | Disable sitemap discovery | `False` |
| `--user-agent` | Custom User-Agent header | *Chrome 124 Crawlee* |

---

## 🌐 Streamlit Web Application

Launch the interactive web UI:

```bash
streamlit run app.py
```

### Features:
- **Interactive Configuration**: Adjust pages, depth, concurrency, delay, and policies.
- **Live Progress Tracking**: Real-time progress bar, latency metrics, word counts, and event logs.
- **Dataset Inspector**:
  - Multi-tab inspector for Rendered HTML, Cleaned HTML, Markdown, Plain Text, Structured Sections, Images, Links, Tables, Metadata & JSON-LD, Diagnostics, and Full JSON.
- **1-Click Exports**: Export complete datasets in **JSONL**, **JSON**, **CSV**, or **Excel (.xlsx)** format.

---

## ⚖️ Important Policies & Limitations

1. **Authorized Crawling Only**: This software is intended solely for authorized data collection compliant with the target site's Terms of Service and `robots.txt`.
2. **No Access Control Bypass**: The crawler does not defeat CAPTCHAs, bypass authentication, solve Cloudflare Turnstile, or circumvent paywalls. When an access challenge or denial is detected, the record is flagged as `BLOCKED`.
3. **Derived vs. Original**: The `rendered_html` represents the source of truth from browser execution. `cleaned_html`, `markdown`, and `plain_text` are derived formats for analytical convenience.

# Production Web Content Crawler & Extraction Engine

High-performance, accuracy-focused web crawling application built on **Crawlee for Python + PlaywrightCrawler (Headless Chromium)**.

---

## 🧱 5-Layer Architecture

1. **Layer 1: Browser Fetcher & Renderer (`layer1_fetcher.py`)**
   - Headless Chromium with JavaScript execution and hydration.
   - Viewport scroll triggering for lazy-loaded content.
   - Strict SSRF protection and access challenge detection.

2. **Layer 2: Exact Lossless Extractor (`layer2_extractor.py`)**
   - 100% complete content preservation (zero truncation, zero missing sections).
   - Multi-representation: Rendered HTML, Formatted Markdown, and Plain Text.

3. **Layer 3: URL Frontier & Politeness (`layer3_frontier.py`)**
   - URL normalization and tracking parameter stripping (`utm_*`, `gclid`, `fbclid`, `nvid`, etc.).
   - Canonical query parameter sorting.
   - Domain boundary scope enforcement.
   - `robots.txt` parsing and automatic `Crawl-delay` rate limiting.
   - XML sitemap discovery (`sitemap.xml`, `sitemap_index.xml`).

4. **Layer 4: Multi-Page Crawlee Engine (`crawler.py`)**
   - Multi-worker concurrent crawling via Crawlee `RequestQueue`.
   - Depth bounds and max page limits.
   - Real-time progress streaming and honest limit reporting (`COMPLETED_ALL_DISCOVERED` vs `PARTIAL_LIMIT_REACHED`).

5. **Layer 5: Minimal UI & Export Engine (`app.py`)**
   - Ultra-clean Streamlit UI (single URL input and **Extract** button).
   - Multi-tab content inspector.
   - 1-click dataset export (JSONL, JSON, CSV, Excel).

---

## 🚀 Quickstart

### 1. Install Dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Run Single-Page Fetch & Extraction (Layers 1 & 2)
```bash
python layer1_fetcher.py https://www.nvidia.com/en-in/
python layer2_extractor.py https://www.nvidia.com/en-in/
```

### 3. Run CLI Multi-Page Crawler (Layer 4)
```bash
python crawler.py https://www.nvidia.com/en-in/ --max-pages 10 --concurrency 2 --output output.jsonl
```

### 4. Launch Streamlit Web UI (Layer 5)
```bash
streamlit run app.py
```

### 5. Run Test Suite
```bash
python test_all_layers.py
```

# Production-Grade Web Crawler

An enterprise-ready Python web crawler built for high throughput and anti-bot resistance.

## Key Features

1. **Dual-Tier Traversal Architecture:**
   - **Fast Tier (`curl_cffi`):** High-speed HTTP crawling with native Chrome TLS/JA3/JA4 and HTTP/2 handshake impersonation (0.05s per page).
   - **Stealth Tier (`Crawlee` + Playwright):** Automated headless browser with anti-detect hooks for client-side JavaScript rendering (React/Vue/Angular) and dynamic challenge screens.
2. **Auto-Discovery:** Automatically checks and extracts pages via `sitemap.xml` / `sitemap_index.xml`.
3. **Structured Content Extraction:** Automatically strips boilerplate noise (`nav`, `footer`, cookie banners, scripts, ads) and exports clean, structured Markdown and JSON.
4. **Adaptive Flow Control:** Configurable concurrency semaphores and polite delays to prevent rate-limit bans.

---

## Setup

1. **Navigate to project folder:**
   ```bash
   cd C:\Users\Telan\.gemini\antigravity\scratch\production_web_crawler
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

---

## Usage

### 1. Auto Mode (Recommended)
Automatically searches for sitemaps for fast extraction; falls back to stealth Playwright if dynamic spidering is required:
```bash
python crawler.py https://quotes.toscrape.com/js/ --mode auto --max-pages 25 --output quotes.json
```

### 2. Stealth Mode (For Heavy JS & SPAs)
Runs full Playwright browser with same-domain link discovery:
```bash
python crawler.py https://quotes.toscrape.com/js/ --mode stealth --max-pages 50 --concurrency 4
```

### 3. Fast Mode (Direct TLS Impersonation)
Extracts via high-speed TLS-impersonated HTTP client:
```bash
python crawler.py https://example.com --mode fast --concurrency 10
```

---

## Output Schema (`output.json`)

```json
[
  {
    "url": "https://quotes.toscrape.com/js/",
    "title": "Quotes to Scrape",
    "markdown": "# Quotes to Scrape\n\n“The world as we have created it is a process of our thinking...”\nby Albert Einstein\n...",
    "status": "SUCCESS"
  }
]
```
"# test_crawl" 

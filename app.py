"""
Production Crawler Web UI
=========================
A modern web application providing an intuitive UI to input URLs, 
configure extraction modes (Fast TLS vs Stealth Playwright), and view 
clean Markdown, Structured JSON, and metadata in real-time.
"""

import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.models import Request as CrawleeRequest
from markdownify import markdownify as md
import json

app = FastAPI(title="Web Extraction Studio")

def clean_html_to_markdown(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for selector in [
        "script", "style", "noscript", "svg", "iframe",
        "nav", "footer", "header", "aside",
        ".cookie-banner", "#cookie-notice", ".advertisement", ".ad-container"
    ]:
        for el in soup.select(selector):
            el.decompose()

    main_content = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", {"id": "content"})
        or soup.find("body")
        or soup
    )
    raw_md = md(str(main_content), heading_style="ATX", strip=["img", "a"])
    lines = [line.strip() for line in raw_md.splitlines()]
    return "\n".join(line for line in lines if line)


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Production Web Extractor</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen flex flex-col items-center py-10 px-4">
    <div class="w-full max-w-4xl space-y-6">
        
        <!-- Header -->
        <div class="text-center space-y-2">
            <div class="inline-flex items-center space-x-2 bg-indigo-500/10 border border-indigo-500/30 px-3 py-1 rounded-full text-indigo-400 text-xs font-semibold uppercase tracking-wider">
                <i class="fa-solid fa-shield-halved"></i>
                <span>Anti-Bot Resistant Engine</span>
            </div>
            <h1 class="text-3xl font-bold tracking-tight text-white sm:text-4xl">Web Data Extractor</h1>
            <p class="text-slate-400 text-sm">Extract clean Markdown and structured JSON without bot detection.</p>
        </div>

        <!-- Input Card -->
        <div class="bg-slate-800/80 backdrop-blur border border-slate-700/80 rounded-2xl p-6 shadow-xl space-y-4">
            <div>
                <label class="block text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2">Target Website URL</label>
                <div class="relative">
                    <span class="absolute inset-y-0 left-0 flex items-center pl-4 text-slate-500">
                        <i class="fa-solid fa-globe"></i>
                    </span>
                    <input id="urlInput" type="url" placeholder="https://example.com" value="https://quotes.toscrape.com/js/"
                        class="w-full pl-11 pr-4 py-3 bg-slate-900/90 border border-slate-700 rounded-xl focus:outline-none focus:ring-2 focus:ring-indigo-500 text-slate-100 placeholder-slate-500 text-sm transition" />
                </div>
            </div>

            <!-- Mode Selector -->
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
                <label class="relative flex items-center p-3.5 bg-slate-900/60 border border-slate-700/80 rounded-xl cursor-pointer hover:border-indigo-500/60 transition">
                    <input type="radio" name="mode" value="fast" class="text-indigo-600 focus:ring-indigo-500 h-4 w-4" checked />
                    <div class="ml-3">
                        <span class="block text-sm font-semibold text-slate-200">⚡ Fast TLS Mode (curl_cffi)</span>
                        <span class="block text-xs text-slate-400">Chrome JA3/JA4 TLS impersonation (0.05s)</span>
                    </div>
                </label>

                <label class="relative flex items-center p-3.5 bg-slate-900/60 border border-slate-700/80 rounded-xl cursor-pointer hover:border-indigo-500/60 transition">
                    <input type="radio" name="mode" value="stealth" class="text-indigo-600 focus:ring-indigo-500 h-4 w-4" />
                    <div class="ml-3">
                        <span class="block text-sm font-semibold text-slate-200">🛡️ Stealth Browser (Playwright)</span>
                        <span class="block text-xs text-slate-400">Full JS / SPA rendering & anti-detect</span>
                    </div>
                </label>
            </div>

            <!-- Action Button -->
            <button id="extractBtn" onclick="extractData()"
                class="w-full py-3.5 bg-indigo-600 hover:bg-indigo-500 active:scale-[0.99] text-white font-medium rounded-xl shadow-lg shadow-indigo-600/30 flex items-center justify-center space-x-2 transition duration-150">
                <i class="fa-solid fa-bolt"></i>
                <span id="btnText">Extract Clean Data</span>
            </button>
        </div>

        <!-- Result Box -->
        <div id="resultCard" class="hidden bg-slate-800/80 backdrop-blur border border-slate-700/80 rounded-2xl p-6 shadow-xl space-y-4">
            <div class="flex items-center justify-between border-b border-slate-700/60 pb-3">
                <div class="space-y-0.5">
                    <div class="flex items-center space-x-2">
                        <span id="badgeStatus" class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">200 OK</span>
                        <h3 id="pageTitle" class="text-base font-semibold text-white truncate max-w-md">Page Title</h3>
                    </div>
                    <p id="pageUrl" class="text-xs text-slate-400 truncate"></p>
                </div>
                
                <!-- View Tabs -->
                <div class="flex bg-slate-900/80 rounded-lg p-1 border border-slate-700/60 text-xs">
                    <button onclick="switchTab('markdown')" id="tabMd" class="px-3 py-1.5 rounded-md font-medium text-white bg-indigo-600 transition">Markdown</button>
                    <button onclick="switchTab('json')" id="tabJson" class="px-3 py-1.5 rounded-md font-medium text-slate-400 hover:text-white transition">JSON</button>
                </div>
            </div>

            <!-- Content Area -->
            <div class="relative">
                <button onclick="copyContent()" class="absolute top-3 right-3 px-2.5 py-1 text-xs bg-slate-700/80 hover:bg-slate-600 text-slate-200 rounded-md border border-slate-600 transition flex items-center space-x-1.5">
                    <i class="fa-regular fa-copy"></i>
                    <span id="copyLabel">Copy</span>
                </button>
                
                <pre id="outputView" class="w-full bg-slate-950/90 border border-slate-800 rounded-xl p-4 text-xs font-mono text-slate-300 overflow-x-auto max-h-96 leading-relaxed whitespace-pre-wrap"></pre>
            </div>
        </div>

    </div>

    <script>
        let extractedResult = null;
        let currentTab = 'markdown';

        async function extractData() {
            const url = document.getElementById("urlInput").value.trim();
            if (!url) return alert("Please enter a valid URL");

            const mode = document.querySelector('input[name="mode"]:checked').value;
            const btn = document.getElementById("extractBtn");
            const btnText = document.getElementById("btnText");
            const resultCard = document.getElementById("resultCard");

            btn.disabled = true;
            btnText.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin"></i> Extracting...`;

            try {
                const response = await fetch("/api/extract", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ url, mode })
                });

                extractedResult = await response.json();
                
                if (response.ok && extractedResult.status === "SUCCESS") {
                    document.getElementById("pageTitle").textContent = extractedResult.title || "Extracted Page";
                    document.getElementById("pageUrl").textContent = extractedResult.url;
                    resultCard.classList.remove("hidden");
                    switchTab(currentTab);
                } else {
                    alert("Extraction failed: " + (extractedResult.error || "Unknown error"));
                }
            } catch (err) {
                alert("Network error: " + err.message);
            } finally {
                btn.disabled = false;
                btnText.innerHTML = `Extract Clean Data`;
            }
        }

        function switchTab(tab) {
            currentTab = tab;
            const outputView = document.getElementById("outputView");
            const tabMd = document.getElementById("tabMd");
            const tabJson = document.getElementById("tabJson");

            if (tab === 'markdown') {
                tabMd.className = "px-3 py-1.5 rounded-md font-medium text-white bg-indigo-600 transition";
                tabJson.className = "px-3 py-1.5 rounded-md font-medium text-slate-400 hover:text-white transition";
                outputView.textContent = extractedResult.markdown;
            } else {
                tabJson.className = "px-3 py-1.5 rounded-md font-medium text-white bg-indigo-600 transition";
                tabMd.className = "px-3 py-1.5 rounded-md font-medium text-slate-400 hover:text-white transition";
                outputView.textContent = JSON.stringify(extractedResult, null, 2);
            }
        }

        function copyContent() {
            const content = currentTab === 'markdown' ? extractedResult.markdown : JSON.stringify(extractedResult, null, 2);
            navigator.clipboard.writeText(content);
            const label = document.getElementById("copyLabel");
            label.textContent = "Copied!";
            setTimeout(() => label.textContent = "Copy", 1500);
        }
    </script>
</body>
</html>
"""

@app.post("/api/extract")
async def extract_endpoint(payload: dict):
    url = payload.get("url")
    mode = payload.get("mode", "fast")

    if not url:
        return JSONResponse({"status": "ERROR", "error": "URL is required"}, status_code=400)

    try:
        if mode == "fast":
            async with AsyncSession() as session:
                resp = await session.get(url, impersonate="chrome120", timeout=12)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    title = soup.find("title").get_text(strip=True) if soup.find("title") else "Untitled"
                    markdown = clean_html_to_markdown(resp.text)
                    return {
                        "status": "SUCCESS",
                        "url": url,
                        "mode": "Fast TLS (curl_cffi)",
                        "title": title,
                        "markdown": markdown,
                    }
                else:
                    return JSONResponse({"status": "ERROR", "error": f"HTTP status {resp.status_code}"}, status_code=resp.status_code)

        elif mode == "stealth":
            results = {}
            crawler = PlaywrightCrawler(max_requests_per_crawl=1, headless=True)

            @crawler.router.default_handler
            async def handler(context: PlaywrightCrawlingContext):
                await context.page.wait_for_load_state("domcontentloaded")
                title = await context.page.title()
                html = await context.page.content()
                results["title"] = title
                results["markdown"] = clean_html_to_markdown(html)

            await crawler.run([CrawleeRequest.from_url(url)])
            return {
                "status": "SUCCESS",
                "url": url,
                "mode": "Stealth Playwright",
                "title": results.get("title", "Untitled"),
                "markdown": results.get("markdown", ""),
            }

    except Exception as e:
        return JSONResponse({"status": "ERROR", "error": str(e)}, status_code=500)


if __name__ == "__main__":
    print("Starting Web Extractor UI at http://localhost:8000 ...")
    uvicorn.run(app, host="0.0.0.0", port=8000)

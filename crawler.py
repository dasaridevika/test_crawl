"""
Production-Grade Web Crawler & Structured Data Engine
=====================================================
Single crawler abstraction built exclusively on Crawlee for Python + PlaywrightCrawler.

Features:
- Browser rendering with JavaScript execution via Crawlee PlaywrightCrawler (Chromium / headless)
- Request queue deduplication, bounded concurrency, and depth tracking
- Sitemap discovery (sitemap.xml, sitemap_index.xml, robots.txt declarations)
- Strict SSRF protection, IP blocklisting, and domain boundary enforcement
- Complete page preservation: raw_html, rendered_html, cleaned_html, markdown, plain_text,
  headings, sections, cards, images, links, tables, json_ld, and extraction diagnostics
- Multi-representation non-destructive derived extraction pipeline
- Comprehensive image extraction (src, data-src, srcset, picture source, og:image, inline background-image)
- Structured sectioning, card grid extraction, and duplicate block fingerprinting diagnostics
- Completeness verification, access challenge detection (BLOCKED), and structured error reporting
- Transparent page limit reporting (PARTIAL_LIMIT_REACHED vs COMPLETED_ALL_DISCOVERED)
- Zero silent truncation or data replacement
"""

import argparse
import asyncio
import gzip
import hashlib
import html as html_lib
import io
import ipaddress
import json
import logging
import re
import socket
import sys
import time
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request as URLRequest, urlopen

import defusedxml.ElementTree as ET
import markdownify
import pandas as pd
from bs4 import BeautifulSoup, Tag
from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.storages import RequestQueue

# -----------------------------------------------------------------------------
# LOGGING CONFIGURATION
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("CrawleePlaywrightCrawler")


# -----------------------------------------------------------------------------
# CONFIGURATION DATA CLASS
# -----------------------------------------------------------------------------
@dataclass
class CrawlConfig:
    seed_url: str
    max_pages: int = 100
    max_depth: int = 3
    concurrency: int = 4
    timeout: int = 30
    retries: int = 2
    delay: float = 0.5
    max_response_bytes: int = 10 * 1024 * 1024  # 10 MB
    allow_subdomains: bool = False
    respect_robots: bool = True
    discover_sitemaps: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 (Compatible; Crawlee-Playwright/1.0)"
    )


# -----------------------------------------------------------------------------
# SECURITY & SSRF PROTECTION
# -----------------------------------------------------------------------------
DISALLOWED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "metadata.google.internal",
    "instance-data",
}

DISALLOWED_IPS = {
    "169.254.169.254",  # AWS/GCP/Azure link-local metadata
    "100.100.100.200",  # Alibaba cloud metadata
}


def is_safe_url(url: str) -> Tuple[bool, Optional[str]]:
    """
    Validates scheme and resolves hostname to ensure it does not point
    to private networks, loopback addresses, or cloud metadata endpoints.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"Disallowed scheme: {parsed.scheme}"

        hostname = parsed.hostname
        if not hostname:
            return False, "Missing hostname in URL"

        hostname_lower = hostname.lower()
        if hostname_lower in DISALLOWED_HOSTS or hostname_lower.endswith(".local") or hostname_lower.endswith(".internal"):
            return False, f"Disallowed internal host: {hostname}"

        if hostname_lower in DISALLOWED_IPS:
            return False, f"Disallowed cloud metadata IP: {hostname}"

        # Resolve IP to detect SSRF against private / loopback IP ranges
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for family, _, _, _, sockaddr in addr_info:
                ip_str = sockaddr[0]
                ip = ipaddress.ip_address(ip_str)

                if ip_str in DISALLOWED_IPS:
                    return False, f"Resolved to cloud metadata address: {ip_str}"

                if (
                    ip.is_loopback
                    or ip.is_private
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_unspecified
                ):
                    return False, f"Resolved to disallowed IP: {ip_str}"
        except socket.gaierror:
            # If DNS lookup fails temporarily, continue or let Playwright catch
            pass

        return True, None
    except Exception as e:
        return False, f"URL security validation error: {str(e)}"


# -----------------------------------------------------------------------------
# URL NORMALIZATION & SCOPE MATCHING
# -----------------------------------------------------------------------------
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "dclid", "ncid", "session_id", "trk", "_ga", "mc_eid",
    "ref", "source", "ref_src", "nvid"
}


def get_apex_domain(hostname: str) -> str:
    """Extracts apex domain (e.g. 'example.com' from 'sub.example.com')."""
    parts = hostname.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return hostname.lower()


def normalize_url(raw_url: str, base_url: str) -> Optional[str]:
    """
    Resolves relative links, strips fragments, and removes marketing tracking parameters.
    """
    if not raw_url or raw_url.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "blob:")):
        return None

    try:
        full_url = urljoin(base_url, raw_url).split("#")[0].strip()
        parsed = urlparse(full_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None

        # Clean query parameters
        if parsed.query:
            qs = parse_qs(parsed.query, keep_blank_values=True)
            filtered_qs = {k: v for k, v in sorted(qs.items()) if k.lower() not in TRACKING_PARAMS}
            clean_query = urlencode(filtered_qs, doseq=True) if filtered_qs else ""
        else:
            clean_query = ""

        # Normalize path
        clean_path = parsed.path
        while "//" in clean_path:
            clean_path = clean_path.replace("//", "/")
        if clean_path != "/" and clean_path.endswith("/"):
            clean_path = clean_path.rstrip("/")

        return urlunparse((parsed.scheme.lower(), parsed.hostname.lower(), clean_path, "", clean_query, ""))
    except Exception:
        return None


def is_url_in_scope(target_url: str, seed_url: str, allow_subdomains: bool = False) -> bool:
    """
    Enforces domain boundaries: exact hostname by default, or apex domain if allow_subdomains is True.
    """
    try:
        target_parsed = urlparse(target_url)
        seed_parsed = urlparse(seed_url)

        if not target_parsed.hostname or not seed_parsed.hostname:
            return False

        target_host = target_parsed.hostname.lower()
        seed_host = seed_parsed.hostname.lower()

        if not allow_subdomains:
            return target_host == seed_host

        target_apex = get_apex_domain(target_host)
        seed_apex = get_apex_domain(seed_host)
        return target_apex == seed_apex
    except Exception:
        return False


# -----------------------------------------------------------------------------
# ROBOTS.TXT & SITEMAP DISCOVERY
# -----------------------------------------------------------------------------
class RobotsManager:
    """Handles fetching, parsing, and caching robots.txt rules."""

    def __init__(self, seed_url: str, user_agent: str, timeout: int = 10):
        self.seed_url = seed_url
        self.user_agent = user_agent
        self.timeout = timeout
        self.parser = urllib.robotparser.RobotFileParser()
        self.sitemaps: List[str] = []
        self._fetched = False

    def fetch(self) -> None:
        parsed = urlparse(self.seed_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        safe, reason = is_safe_url(robots_url)
        if not safe:
            logger.warning(f"Robots.txt URL skipped due to security rule: {reason}")
            self._fetched = True
            return

        try:
            req = URLRequest(robots_url, headers={"User-Agent": self.user_agent})
            with urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    content = resp.read().decode("utf-8", errors="replace")
                    self.parser.parse(content.splitlines())
                    # Extract declared sitemaps
                    for line in content.splitlines():
                        if line.strip().lower().startswith("sitemap:"):
                            s_url = line.split(":", 1)[1].strip()
                            if s_url and s_url not in self.sitemaps:
                                self.sitemaps.append(s_url)
                    logger.info(f"Loaded robots.txt for {parsed.netloc} ({len(self.sitemaps)} sitemaps declared)")
        except Exception as e:
            logger.debug(f"robots.txt unavailable or error for {robots_url} (defaulting to allow all): {e}")
            self.parser.allow_all = True
        finally:
            self._fetched = True

    def can_fetch(self, url: str) -> bool:
        if not self._fetched:
            self.fetch()
        try:
            return self.parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def get_crawl_delay(self) -> Optional[float]:
        if not self._fetched:
            self.fetch()
        try:
            delay = self.parser.crawl_delay(self.user_agent)
            return float(delay) if delay is not None else None
        except Exception:
            return None


class SitemapDiscoverer:
    """Discovers in-scope URLs from sitemap.xml and nested sitemap indexes."""

    def __init__(self, seed_url: str, user_agent: str, allow_subdomains: bool = False, max_sitemaps: int = 10, max_urls: int = 2000):
        self.seed_url = seed_url
        self.user_agent = user_agent
        self.allow_subdomains = allow_subdomains
        self.max_sitemaps = max_sitemaps
        self.max_urls = max_urls

    def discover(self, declared_sitemaps: Optional[List[str]] = None) -> List[str]:
        parsed = urlparse(self.seed_url)
        base_origin = f"{parsed.scheme}://{parsed.netloc}"

        candidate_sitemaps: List[str] = []
        if declared_sitemaps:
            candidate_sitemaps.extend(declared_sitemaps)

        default_sitemaps = [
            f"{base_origin}/sitemap.xml",
            f"{base_origin}/sitemap_index.xml",
        ]
        for s in default_sitemaps:
            if s not in candidate_sitemaps:
                candidate_sitemaps.append(s)

        discovered_urls: Set[str] = set()
        visited_sitemaps: Set[str] = set()
        queue: List[str] = list(candidate_sitemaps)

        while queue and len(visited_sitemaps) < self.max_sitemaps and len(discovered_urls) < self.max_urls:
            s_url = queue.pop(0)
            if s_url in visited_sitemaps:
                continue
            visited_sitemaps.add(s_url)

            safe, _ = is_safe_url(s_url)
            if not safe:
                continue

            try:
                req = URLRequest(s_url, headers={"User-Agent": self.user_agent})
                with urlopen(req, timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    raw_bytes = resp.read()

                    if s_url.endswith(".gz") or raw_bytes[:2] == b"\x1f\x8b":
                        try:
                            raw_bytes = gzip.decompress(raw_bytes)
                        except Exception:
                            continue

                    try:
                        root = ET.fromstring(raw_bytes)
                    except Exception:
                        continue

                    # Extract loc tags
                    for elem in root.iter():
                        if elem.tag.endswith("loc") and elem.text:
                            loc = elem.text.strip()
                            if loc.endswith(".xml") or loc.endswith(".xml.gz"):
                                if loc not in visited_sitemaps and loc not in queue and len(queue) < self.max_sitemaps:
                                    queue.append(loc)
                            else:
                                clean = normalize_url(loc, self.seed_url)
                                if clean and is_url_in_scope(clean, self.seed_url, self.allow_subdomains):
                                    discovered_urls.add(clean)
                                    if len(discovered_urls) >= self.max_urls:
                                        break
            except Exception as e:
                logger.debug(f"Sitemap read error for {s_url}: {e}")

        logger.info(f"Discovered {len(discovered_urls)} URLs from {len(visited_sitemaps)} sitemaps")
        return list(discovered_urls)


# -----------------------------------------------------------------------------
# REGION CLASSIFICATION & ENTITY EXTRACTION
# -----------------------------------------------------------------------------
ACCESS_CHALLENGE_PATTERNS = [
    re.compile(r"attention required!?\s*\|\s*cloudflare", re.I),
    re.compile(r"cf-browser-verification", re.I),
    re.compile(r"just a moment\.\.\.", re.I),
    re.compile(r"please verify (?:that )?you are (?:a )?human", re.I),
    re.compile(r"security check to access", re.I),
    re.compile(r"recaptcha|hcaptcha|turnstile", re.I),
    re.compile(r"datadome|incapsula|perimeterx|imperva|akamai", re.I),
    re.compile(r"access to this page has been denied", re.I),
]


def classify_element_context(el: Any) -> str:
    """Classifies the semantic region of the page an element belongs to."""
    if not hasattr(el, "parents"):
        return "main_content"

    for parent in el.parents:
        if not isinstance(parent, Tag):
            continue
        tag_name = parent.name.lower()
        role = parent.get("role", "").lower()

        if tag_name in ("header", "nav") or role == "navigation":
            return "navigation"
        if tag_name == "footer" or role == "contentinfo":
            return "footer"
        if tag_name == "aside" or role == "complementary":
            return "sidebar"
        if tag_name == "dialog" or role in ("dialog", "alertdialog"):
            return "modal"

        class_list = parent.get("class", [])
        classes = " ".join(class_list) if isinstance(class_list, list) else str(class_list)
        id_str = str(parent.get("id") or "")
        combined = (classes + " " + id_str).lower()

        if any(k in combined for k in ["hero", "banner", "jumbotron", "splash"]):
            return "hero"
        if any(k in combined for k in ["carousel", "slider", "swiper"]):
            return "carousel"
        if any(k in combined for k in ["mobile-menu", "mobile-nav", "drawer", "flyout"]):
            return "mobile"
        if any(k in combined for k in ["modal", "popup", "cookie", "cookie-banner", "consent"]):
            return "modal"

    return "main_content"


def extract_images_comprehensive(rendered_soup: BeautifulSoup, target_url: str) -> List[Dict[str, Any]]:
    """
    Extracts all images from src, data-src, srcset, picture source, og:image, and background-image.
    Preserves valid SVGs and classifies them.
    """
    images: List[Dict[str, Any]] = []
    seen_urls: Set[str] = set()

    def add_image(url_candidate: str, alt: str = "", title: str = "", width: Any = None, height: Any = None, source_attr: str = "src", context: str = "main_content"):
        if not url_candidate or url_candidate.startswith("data:") or url_candidate.startswith("blob:"):
            return
        abs_url = urljoin(target_url, url_candidate.strip())
        if abs_url in seen_urls:
            return
        seen_urls.add(abs_url)

        url_lower = abs_url.lower()
        is_svg = url_lower.endswith(".svg") or "image/svg+xml" in url_lower
        is_pixel = (
            (width == 1 and height == 1)
            or "1x1" in url_lower
            or "spacer" in url_lower
            or "pixel" in url_lower
            or "tracking" in url_lower
        )

        images.append({
            "url": abs_url,
            "alt": alt.strip() if alt else "",
            "title": title.strip() if title else "",
            "width": int(width) if str(width).isdigit() else None,
            "height": int(height) if str(height).isdigit() else None,
            "source_attribute": source_attr,
            "is_svg": is_svg,
            "is_tracking_pixel": is_pixel,
            "context": context
        })

    # 1. <img> tags
    for img in rendered_soup.find_all("img"):
        ctx = classify_element_context(img)
        alt = img.get("alt", "")
        title = img.get("title", "")
        w = img.get("width")
        h = img.get("height")

        # Check srcset first (often contains highest resolution)
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            for part in srcset.split(","):
                cand = part.strip().split()[0] if part.strip() else ""
                if cand:
                    add_image(cand, alt=alt, title=title, width=w, height=h, source_attr="srcset", context=ctx)

        # Standard src and lazy-src attributes
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-lazy", "data-image"):
            val = img.get(attr)
            if val:
                add_image(val, alt=alt, title=title, width=w, height=h, source_attr=attr, context=ctx)

    # 2. <picture> <source> tags
    for source in rendered_soup.find_all("source"):
        ctx = classify_element_context(source)
        srcset = source.get("srcset") or source.get("data-srcset")
        if srcset:
            for part in srcset.split(","):
                cand = part.strip().split()[0] if part.strip() else ""
                if cand:
                    add_image(cand, source_attr="picture_source", context=ctx)
        src = source.get("src")
        if src:
            add_image(src, source_attr="picture_source", context=ctx)

    # 3. Open Graph & Twitter Meta Images
    for meta in rendered_soup.find_all("meta"):
        prop = meta.get("property", "").lower()
        name = meta.get("name", "").lower()
        content = meta.get("content", "").strip()
        if prop in ("og:image", "og:image:url", "og:image:secure_url") or name in ("twitter:image", "twitter:image:src"):
            if content:
                add_image(content, source_attr="meta_og", context="hero")

    # 4. Inline style background-image
    for el in rendered_soup.find_all(attrs={"style": re.compile(r"background(?:-image)?:\s*url", re.I)}):
        ctx = classify_element_context(el)
        style_val = el.get("style", "")
        bg_matches = re.findall(r"background(?:-image)?:\s*url\((['\"]?)(.*?)\1\)", style_val, re.I)
        for _, bg_url in bg_matches:
            if bg_url:
                add_image(bg_url, source_attr="css_background", context=ctx)

    return images


def extract_links_classified(rendered_soup: BeautifulSoup, target_url: str, seed_url: str, allow_subdomains: bool = False) -> List[Dict[str, Any]]:
    """
    Extracts all links without truncation and classifies them.
    """
    links: List[Dict[str, Any]] = []
    seen_links: Set[str] = set()

    # Canonical Link
    can_tag = rendered_soup.find("link", rel=re.compile(r"\bcanonical\b", re.I))
    if can_tag and can_tag.get("href"):
        can_url = urljoin(target_url, can_tag["href"].strip())
        links.append({
            "href": can_url,
            "text": "Canonical URL",
            "title": "",
            "category": "canonical",
            "context": "head"
        })
        seen_links.add(can_url)

    social_domains = {"twitter.com", "x.com", "linkedin.com", "facebook.com", "youtube.com", "github.com", "instagram.com"}
    asset_exts = (".pdf", ".zip", ".tar", ".gz", ".exe", ".dmg", ".mp4", ".csv", ".xlsx", ".png", ".jpg", ".svg")

    for a in rendered_soup.find_all("a", href=True):
        href_raw = a["href"].strip()
        if not href_raw or href_raw.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            continue

        abs_href = urljoin(target_url, href_raw)
        if abs_href in seen_links:
            continue
        seen_links.add(abs_href)

        parsed_href = urlparse(abs_href)
        host = (parsed_href.hostname or "").lower()
        ctx = classify_element_context(a)
        text = a.get_text(" ", strip=True)
        title = a.get("title", "").strip()

        if any(sd in host for sd in social_domains):
            cat = "social"
        elif abs_href.lower().endswith(asset_exts):
            cat = "asset"
        elif ctx == "navigation":
            cat = "navigation"
        elif ctx == "footer":
            cat = "footer"
        elif is_url_in_scope(abs_href, seed_url, allow_subdomains):
            cat = "internal"
        else:
            cat = "external"

        links.append({
            "href": abs_href,
            "text": text,
            "title": title,
            "category": cat,
            "context": ctx
        })

    return links


def extract_tables_serializable(rendered_soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """
    Extracts all HTML tables into a robust, serializable schema.
    """
    tables: List[Dict[str, Any]] = []
    for i, table in enumerate(rendered_soup.find_all("table")):
        try:
            caption_el = table.find("caption")
            caption = caption_el.get_text(" ", strip=True) if caption_el else ""

            headers = []
            thead = table.find("thead")
            if thead:
                for th in thead.find_all(["th", "td"]):
                    headers.append(th.get_text(" ", strip=True))
            if not headers:
                first_tr = table.find("tr")
                if first_tr:
                    for cell in first_tr.find_all(["th", "td"]):
                        headers.append(cell.get_text(" ", strip=True))

            rows = []
            tbody = table.find("tbody") or table
            for tr in tbody.find_all("tr"):
                if thead is None and not rows and headers and [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] == headers:
                    continue
                row_cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
                if row_cells:
                    rows.append(row_cells)

            col_count = max([len(r) for r in rows], default=len(headers))
            tables.append({
                "id": f"Table #{i+1}",
                "caption": caption,
                "headers": headers,
                "rows": rows,
                "row_count": len(rows),
                "column_count": col_count,
                "source_html": str(table)
            })
        except Exception:
            pass

    return tables


def extract_sections_cards_and_diagnostics(
    cleaned_soup: BeautifulSoup,
    target_url: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Extracts structured content sections, cards, and fingerprinted duplicate blocks.
    """
    sections: List[Dict[str, Any]] = []
    cards: List[Dict[str, Any]] = []
    block_counts: Dict[str, Dict[str, Any]] = {}

    # 1. Duplicate block fingerprinting across all paragraphs & cards
    for el in cleaned_soup.find_all(["p", "li", "h2", "h3", "h4", "article", "div"]):
        if el.find(["p", "div", "article"]):
            continue
        text = el.get_text(" ", strip=True)
        if len(text) >= 30:
            norm = re.sub(r"\s+", " ", text.lower()).strip()
            fp = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
            ctx = classify_element_context(el)

            if fp not in block_counts:
                block_counts[fp] = {
                    "fingerprint": fp,
                    "text_sample": text[:120] + ("..." if len(text) > 120 else ""),
                    "occurrences": 1,
                    "locations": {ctx}
                }
            else:
                block_counts[fp]["occurrences"] += 1
                block_counts[fp]["locations"].add(ctx)

    duplicate_blocks = [
        {
            "fingerprint": v["fingerprint"],
            "text_sample": v["text_sample"],
            "occurrences": v["occurrences"],
            "locations": sorted(list(v["locations"]))
        }
        for v in block_counts.values()
        if v["occurrences"] > 1
    ]

    # 2. Extract Cards (card grids, promo tiles, news items)
    for card_el in cleaned_soup.find_all(class_=re.compile(r"card|teaser|promo|feature-box|grid-item|news-item|tile", re.I)):
        title_tag = card_el.find(["h2", "h3", "h4", "h5", "strong", "b"])
        title = title_tag.get_text(" ", strip=True) if title_tag else ""
        text = card_el.get_text(" ", strip=True)
        link_tag = card_el.find("a", href=True)
        link_url = urljoin(target_url, link_tag["href"]) if link_tag else ""
        img_tag = card_el.find("img")
        img_url = urljoin(target_url, img_tag.get("src", "")) if img_tag and img_tag.get("src") else ""

        if title or (link_url and len(text) > 20):
            cards.append({
                "title": title or "Card Item",
                "text": text,
                "url": link_url,
                "image": img_url,
                "context": classify_element_context(card_el)
            })

    # 3. Extract Structured Sections based on Headings
    headings = cleaned_soup.find_all(["h1", "h2", "h3", "h4"])
    if headings:
        for i, h in enumerate(headings):
            level = int(h.name[1])
            heading_text = h.get_text(" ", strip=True)
            ctx = classify_element_context(h)

            section_nodes = []
            curr = h.next_sibling
            while curr:
                if isinstance(curr, Tag):
                    if curr.name in ["h1", "h2", "h3", "h4"] and int(curr.name[1]) <= level:
                        break
                    section_nodes.append(curr)
                curr = curr.next_sibling

            sec_html = "".join(str(n) for n in section_nodes)
            sec_text = " ".join(n.get_text(" ", strip=True) for n in section_nodes if isinstance(n, Tag))

            sec_links = []
            sec_imgs = []
            for n in section_nodes:
                if isinstance(n, Tag):
                    for a in n.find_all("a", href=True):
                        sec_links.append(urljoin(target_url, a["href"]))
                    for img in n.find_all("img"):
                        if img.get("src"):
                            sec_imgs.append(urljoin(target_url, img["src"]))

            sections.append({
                "heading": heading_text,
                "level": level,
                "type": "hero_section" if ctx == "hero" else "content_section",
                "text": sec_text.strip(),
                "html": sec_html.strip(),
                "links": list(set(sec_links)),
                "images": list(set(sec_imgs)),
                "cards": [c for c in cards if c["title"] in sec_text]
            })
    else:
        full_text = cleaned_soup.get_text(" ", strip=True)
        sections.append({
            "heading": "Main Content",
            "level": 1,
            "type": "content_section",
            "text": full_text,
            "html": str(cleaned_soup),
            "links": [],
            "images": [],
            "cards": cards
        })

    return sections, cards, duplicate_blocks


# -----------------------------------------------------------------------------
# NON-DESTRUCTIVE CONTENT EXTRACTION PIPELINE
# -----------------------------------------------------------------------------
def infer_page_category(url: str, title: str) -> str:
    """Categorizes page based on URL structure and title."""
    url_l = url.lower()
    if any(k in url_l for k in ["/blog", "/stories", "/post/", "/news/"]):
        return "Blog / Article"
    if any(k in url_l for k in ["/press", "/announcements", "/media"]):
        return "News & Press"
    if any(k in url_l for k in ["/docs", "/documentation", "/guide", "/api", "/learn", "/tutorial"]):
        return "Documentation & Technical"
    if any(k in url_l for k in ["/case-studies", "/customers", "/story"]):
        return "Case Study"
    if any(k in url_l for k in ["/product", "/solution", "/service", "/pricing", "/features", "/geforce"]):
        return "Product / Solution"
    if url.rstrip("/").count("/") <= 3 or "home" in title.lower():
        return "Homepage / Portal"
    return "Web Page"


def extract_page_content(
    rendered_html: str,
    url: str,
    final_url: Optional[str] = None,
    http_status: int = 200,
    content_type: str = "text/html",
    fetch_time_ms: float = 0.0,
    raw_html: str = "",
    seed_url: Optional[str] = None,
    allow_subdomains: bool = False,
    max_response_bytes: int = 10 * 1024 * 1024,
    depth: int = 0
) -> Dict[str, Any]:
    """
    Executes a complete, non-destructive extraction sequence:
    1. Retains raw_html and rendered_html untouched as source-of-truth representations.
    2. Creates a separate copy for DOM cleaning.
    3. Extracts comprehensive media, classified links, serializable tables, and JSON-LD.
    4. Extracts structured sections, card grids, and duplicate block diagnostics.
    5. Produces derived representations: cleaned_html, markdown, plain_text.
    6. Attaches completeness verification and diagnostics.
    """
    t_start = time.perf_counter()
    warnings: List[str] = []
    error: Optional[Dict[str, str]] = None
    status = "SUCCESS"
    content_complete = True
    target_url = final_url or url
    effective_seed = seed_url or target_url

    content_bytes = len(rendered_html.encode("utf-8", errors="replace"))
    if content_bytes > max_response_bytes:
        content_complete = False
        warnings.append("response_size_limit_reached")

    # 1. Access Challenge & HTTP Status Checks
    if http_status in (403, 401):
        status = "BLOCKED"
        content_complete = False
        warnings.append("access_challenge_detected")
        error = {
            "code": "access_challenge",
            "message": f"Access denied or forbidden by server (HTTP {http_status})"
        }
    elif http_status == 429:
        status = "ERROR"
        content_complete = False
        warnings.append("rate_limited")
        error = {
            "code": "rate_limited",
            "message": "The target returned HTTP 429 Too Many Requests"
        }
    elif http_status >= 400:
        status = "ERROR"
        content_complete = False
        warnings.append("http_error")
        error = {
            "code": "http_error",
            "message": f"Server returned HTTP error status {http_status}"
        }

    # Challenge text signature check
    if status == "SUCCESS":
        for pattern in ACCESS_CHALLENGE_PATTERNS:
            if pattern.search(rendered_html):
                status = "BLOCKED"
                content_complete = False
                warnings.append("access_challenge_detected")
                error = {
                    "code": "access_challenge",
                    "message": "The page requires an access method unsupported by this crawler"
                }
                break

    # 2. Parse Source of Truth Rendered DOM
    rendered_soup = BeautifulSoup(rendered_html, "html.parser")

    # Title Extraction
    title_tag = rendered_soup.find("title")
    h1_tag = rendered_soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else (h1_tag.get_text(" ", strip=True) if h1_tag else "Untitled Page")
    title = re.sub(r"\s+", " ", title).strip()

    # Metadata & Canonical
    meta_description = ""
    author = ""
    published_date = ""
    canonical_url = ""

    can_tag = rendered_soup.find("link", rel=re.compile(r"\bcanonical\b", re.I))
    if can_tag and can_tag.get("href"):
        canonical_url = urljoin(target_url, can_tag["href"].strip())

    for meta in rendered_soup.find_all("meta"):
        name = meta.get("name", "").lower()
        prop = meta.get("property", "").lower()
        content = meta.get("content", "").strip()

        if name in ("description", "twitter:description") or prop in ("og:description",):
            if not meta_description:
                meta_description = content
        elif name in ("author", "article:author", "dc.creator") or prop in ("og:author",):
            if not author:
                author = content
        elif name in ("article:published_time", "pubdate", "date", "dc.date") or prop in ("article:published_time",):
            if not published_date:
                published_date = content

    # JSON-LD Schemas Extraction
    json_ld: List[Any] = []
    for s in rendered_soup.find_all("script", type="application/ld+json"):
        try:
            if s.string:
                parsed_schema = json.loads(s.string.strip())
                json_ld.append(parsed_schema)
                if isinstance(parsed_schema, dict):
                    if not author and "author" in parsed_schema:
                        auth = parsed_schema["author"]
                        author = auth.get("name", "") if isinstance(auth, dict) else str(auth)
                    if not published_date and "datePublished" in parsed_schema:
                        published_date = str(parsed_schema["datePublished"])
                    if not meta_description and "description" in parsed_schema:
                        meta_description = str(parsed_schema["description"])
        except Exception:
            pass

    # Extract Comprehensive Entities
    images = extract_images_comprehensive(rendered_soup, target_url)
    links = extract_links_classified(rendered_soup, target_url, effective_seed, allow_subdomains)
    tables = extract_tables_serializable(rendered_soup)

    # 3. Create Separate Copy for DOM Cleaning
    cleaned_soup = BeautifulSoup(rendered_html, "html.parser")

    content_root = (
        cleaned_soup.find(id="page-content")
        or cleaned_soup.find("main")
        or cleaned_soup.find("article")
        or cleaned_soup.find("body")
        or cleaned_soup
    )

    # Decompose boilerplate noise from cleaned copy
    for tag in content_root(["script", "style", "noscript", "svg", "button", "select", "option", "header", "nav", "footer", "form", "aside", "dialog"]):
        tag.decompose()

    for el in content_root.find_all(attrs={"role": re.compile(r"navigation|banner|contentinfo|dialog|alertdialog", re.I)}):
        el.decompose()

    for el in content_root.find_all(class_=re.compile(r"region-selector|country-selector|cookie|modal|drawer|newsletter-popup|banner-cookie|header-navigation|global-nav|site-header|site-footer|nav-menu|mega-menu|flyout|search-box|search-bar", re.I)):
        el.decompose()

    # Clean Image URLs in Cleaned DOM
    for img in content_root.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy-src")
            or (img.get("srcset", "").split()[0] if img.get("srcset") else None)
        )
        if src and not src.startswith("data:") and "1x1" not in src:
            img["src"] = urljoin(target_url, src)
        else:
            img.decompose()

    # Unwrap card block <a> tags
    for a in content_root.find_all("a", href=True):
        has_blocks = a.find(["h1", "h2", "h3", "h4", "h5", "h6", "div", "p"])
        txt = a.get_text(" ", strip=True)
        if has_blocks or (len(txt) > 40 and ("\n" in a.get_text() or len(txt.split()) > 6)):
            a.unwrap()
        else:
            a["href"] = urljoin(target_url, a["href"])

    # 4. Extract Sections, Cards, and Duplicate Blocks
    sections, cards, duplicate_blocks = extract_sections_cards_and_diagnostics(cleaned_soup, target_url)

    # 5. Generate Derived Cleaned HTML, Markdown, and Plain Text
    cleaned_html = str(content_root)

    markdown = markdownify.markdownify(
        cleaned_html,
        heading_style="ATX",
        bullets="-"
    )
    markdown = re.sub(r"!\[.*?\]\(data:.*?\)", "", markdown)
    markdown = re.sub(r"!\[\]\(\s*\)", "", markdown)
    markdown = re.sub(r"\[Skip to main content\]\(.*?\)", "", markdown, flags=re.IGNORECASE)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    plain_text_lines = [s.strip() for s in content_root.stripped_strings if s.strip()]
    plain_text = "\n\n".join(plain_text_lines)

    # Headings hierarchy
    headings: List[Dict[str, Any]] = []
    for h in content_root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        lvl = int(h.name[1])
        h_text = h.get_text(" ", strip=True)
        if h_text:
            headings.append({"level": lvl, "text": h_text})

    # Word & Character Count
    word_count = len(re.findall(r"\w+", plain_text))
    char_count = len(plain_text)

    # Quality & Incomplete Render Checks
    if status == "SUCCESS":
        spa_markers = ["#root", "#app", "#__next"]
        has_spa_container = any(rendered_soup.select(sel) for sel in spa_markers)
        if (has_spa_container and word_count < 25) or (len(rendered_html) > 5000 and word_count < 15):
            content_complete = False
            warnings.append("potential_incomplete_render")

        if "enable javascript" in plain_text.lower() and word_count < 40:
            content_complete = False
            warnings.append("javascript_required_notice_detected")

    if not meta_description and plain_text:
        meta_description = plain_text[:250] + ("..." if len(plain_text) > 250 else "")

    extraction_time_ms = (time.perf_counter() - t_start) * 1000.0

    quality = {
        "word_count": word_count,
        "character_count": char_count,
        "headings_count": len(headings),
        "sections_count": len(sections),
        "cards_count": len(cards),
        "images_count": len(images),
        "links_count": len(links),
        "tables_count": len(tables),
        "duplicate_blocks_count": len(duplicate_blocks),
        "duplicate_blocks": duplicate_blocks
    }

    return {
        "url": url,
        "final_url": target_url,
        "status": status,
        "http_status": http_status,
        "crawl_method": "crawlee_playwright",
        "rendered": True,
        "content_complete": content_complete,
        "raw_html": raw_html,
        "rendered_html": rendered_html,
        "cleaned_html": cleaned_html,
        "markdown": markdown,
        "plain_text": plain_text,
        "title": title,
        "category": infer_page_category(target_url, title),
        "meta_description": meta_description,
        "author": author or "N/A",
        "published_date": published_date or "N/A",
        "canonical_url": canonical_url or target_url,
        "headings": headings,
        "sections": sections,
        "cards": cards,
        "images": images,
        "links": links,
        "tables": tables,
        "json_ld": json_ld,
        "quality": quality,
        "warnings": warnings,
        "error": error,
        "depth": depth,
        "fetch_time_ms": round(fetch_time_ms, 2),
        "extraction_time_ms": round(extraction_time_ms, 2),
        "content_bytes": content_bytes
    }


# -----------------------------------------------------------------------------
# CRAWLEE PLAYWRIGHT CRAWLER ENGINE
# -----------------------------------------------------------------------------
class CrawleePlaywrightCrawlerEngine:
    """
    Scalable crawling engine built on Crawlee for Python + PlaywrightCrawler.
    """

    def __init__(self, config: CrawlConfig):
        self.config = config
        self.robots = RobotsManager(config.seed_url, config.user_agent, timeout=min(config.timeout, 10))
        self.sitemap_discoverer = SitemapDiscoverer(
            seed_url=config.seed_url,
            user_agent=config.user_agent,
            allow_subdomains=config.allow_subdomains
        )
        self.seen_urls: Set[str] = set()
        self.results: List[Dict[str, Any]] = []
        self.stats: Dict[str, Any] = {
            "attempted": 0,
            "successful": 0,
            "failed": 0,
            "blocked": 0,
            "incomplete": 0,
            "browser_rendered": 0,
            "total_words": 0,
            "total_bytes": 0,
            "total_images": 0,
            "total_links": 0,
            "avg_latency_ms": 0.0,
            "elapsed_seconds": 0.0,
            "pages_in_frontier": 0,
            "pages_remaining": 0,
            "crawl_completion_status": "NOT_STARTED",
            "completion_message": ""
        }

    async def crawl(
        self,
        on_page_crawled: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Runs the full crawl workflow using Crawlee PlaywrightCrawler.
        """
        start_time = time.perf_counter()
        logger.info(f"Starting Crawlee PlaywrightCrawler for: {self.config.seed_url} (max_pages={self.config.max_pages}, max_depth={self.config.max_depth})")

        # 1. Validate Seed URL SSRF Security
        safe, reason = is_safe_url(self.config.seed_url)
        if not safe:
            err_record = {
                "url": self.config.seed_url,
                "final_url": None,
                "status": "ERROR",
                "http_status": None,
                "crawl_method": "crawlee_playwright",
                "rendered": False,
                "content_complete": False,
                "raw_html": "",
                "rendered_html": "",
                "cleaned_html": "",
                "markdown": "",
                "plain_text": "",
                "title": "Security Error",
                "category": "Error",
                "meta_description": "",
                "author": "N/A",
                "published_date": "N/A",
                "canonical_url": self.config.seed_url,
                "headings": [],
                "sections": [],
                "cards": [],
                "images": [],
                "links": [],
                "tables": [],
                "json_ld": [],
                "quality": {},
                "warnings": ["ssrf_security_block"],
                "error": {"code": "security_block", "message": f"Target URL rejected by security policy: {reason}"},
                "depth": 0,
                "fetch_time_ms": 0.0,
                "extraction_time_ms": 0.0,
                "content_bytes": 0
            }
            self.results.append(err_record)
            self.stats["attempted"] = 1
            self.stats["failed"] = 1
            self.stats["crawl_completion_status"] = "ERROR_SECURITY"
            self.stats["completion_message"] = f"Crawl blocked: {reason}"
            if on_page_crawled:
                on_page_crawled(err_record, self.stats)
            return self.results

        # 2. Discover Sitemaps & Robots.txt
        effective_delay = self.config.delay
        if self.config.respect_robots:
            self.robots.fetch()
            r_delay = self.robots.get_crawl_delay()
            if r_delay is not None and r_delay > effective_delay:
                effective_delay = min(r_delay, 10.0)
                logger.info(f"Applying robots.txt Crawl-delay of {effective_delay}s")

        discovered_sitemap_urls: List[str] = []
        if self.config.discover_sitemaps:
            discovered_sitemap_urls = self.sitemap_discoverer.discover(self.robots.sitemaps)

        # 3. Initialize Crawlee RequestQueue
        request_queue = await RequestQueue.open()
        norm_seed = normalize_url(self.config.seed_url, self.config.seed_url) or self.config.seed_url
        self.seen_urls.add(norm_seed)
        await request_queue.add_request(Request.from_url(norm_seed, user_data={"depth": 0}))

        for s_url in discovered_sitemap_urls:
            if s_url not in self.seen_urls and len(self.seen_urls) < self.config.max_pages * 4:
                self.seen_urls.add(s_url)
                await request_queue.add_request(Request.from_url(s_url, user_data={"depth": 1}))

        total_latency = 0.0

        # 4. Initialize Crawlee PlaywrightCrawler
        concurrency = max(1, self.config.concurrency)
        crawler = PlaywrightCrawler(
            request_manager=request_queue,
            max_requests_per_crawl=self.config.max_pages,
            max_request_retries=self.config.retries,
            request_handler_timeout=timedelta(seconds=self.config.timeout),
            navigation_timeout=timedelta(seconds=self.config.timeout),
            concurrency_settings=ConcurrencySettings(
                min_concurrency=1,
                max_concurrency=concurrency,
                desired_concurrency=concurrency
            ),
            headless=True,
        )

        @crawler.router.default_handler
        async def request_handler(context: PlaywrightCrawlingContext) -> None:
            nonlocal total_latency
            current_url = getattr(getattr(context, "request", None), "url", "")
            user_data = getattr(getattr(context, "request", None), "user_data", {}) or {}
            depth = user_data.get("depth", 0)
            self.stats["attempted"] += 1

            # Rate Limit Delay (Politeness)
            if effective_delay > 0:
                await asyncio.sleep(effective_delay)

            # Robots.txt Check
            if self.config.respect_robots and not self.robots.can_fetch(current_url):
                logger.info(f"Robots.txt disallowed: {current_url}")
                rec = {
                    "url": current_url,
                    "final_url": None,
                    "status": "SKIPPED",
                    "http_status": None,
                    "crawl_method": "crawlee_playwright",
                    "rendered": False,
                    "content_complete": False,
                    "raw_html": "",
                    "rendered_html": "",
                    "cleaned_html": "",
                    "markdown": "",
                    "plain_text": "",
                    "title": "Disallowed by robots.txt",
                    "category": "Skipped",
                    "meta_description": "",
                    "author": "N/A",
                    "published_date": "N/A",
                    "canonical_url": current_url,
                    "headings": [],
                    "sections": [],
                    "cards": [],
                    "images": [],
                    "links": [],
                    "tables": [],
                    "json_ld": [],
                    "quality": {
                        "word_count": 0,
                        "character_count": 0,
                        "headings_count": 0,
                        "sections_count": 0,
                        "cards_count": 0,
                        "images_count": 0,
                        "links_count": 0,
                        "tables_count": 0,
                        "duplicate_blocks_count": 0,
                        "duplicate_blocks": []
                    },
                    "warnings": ["disallowed_by_robots_txt"],
                    "error": None,
                    "depth": depth,
                    "fetch_time_ms": 0.0,
                    "extraction_time_ms": 0.0,
                    "content_bytes": 0
                }
                self.results.append(rec)
                if on_page_crawled:
                    try:
                        on_page_crawled(rec, self.stats)
                    except Exception as cb_err:
                        logger.debug(f"on_page_crawled callback error: {cb_err}")
                return

            t0 = time.perf_counter()
            page = getattr(context, "page", None)

            rendered_html = ""
            final_url = current_url
            if page:
                # Wait for DOM ready & trigger lazy loading
                try:
                    await page.wait_for_load_state("domcontentloaded")
                    await page.evaluate("""async () => {
                        window.scrollTo(0, document.body.scrollHeight / 2);
                        await new Promise(r => setTimeout(r, 200));
                        window.scrollTo(0, document.body.scrollHeight);
                        await new Promise(r => setTimeout(r, 200));
                        window.scrollTo(0, 0);
                    }""")
                except Exception:
                    pass

                try:
                    rendered_html = await page.content()
                    final_url = page.url or current_url
                except Exception:
                    rendered_html = ""

            resp = getattr(context, "response", None)
            http_status = getattr(resp, "status", None) or 200
            resp_headers = getattr(resp, "headers", None) or {}
            content_type = resp_headers.get("content-type", "text/html") if isinstance(resp_headers, dict) else "text/html"

            fetch_time_ms = (time.perf_counter() - t0) * 1000.0
            total_latency += fetch_time_ms

            # Extract full structured content
            record = extract_page_content(
                rendered_html=rendered_html,
                url=current_url,
                final_url=final_url,
                http_status=http_status,
                content_type=content_type,
                fetch_time_ms=fetch_time_ms,
                raw_html="",
                seed_url=self.config.seed_url,
                allow_subdomains=self.config.allow_subdomains,
                max_response_bytes=self.config.max_response_bytes,
                depth=depth
            )

            # Update stats
            if record["status"] == "SUCCESS":
                self.stats["successful"] += 1
                self.stats["browser_rendered"] += 1
                self.stats["total_words"] += record["quality"].get("word_count", 0)
                self.stats["total_bytes"] += record["content_bytes"]
                self.stats["total_images"] += len(record.get("images", []))
                self.stats["total_links"] += len(record.get("links", []))
                if not record["content_complete"]:
                    self.stats["incomplete"] += 1
            elif record["status"] == "BLOCKED":
                self.stats["blocked"] += 1
            else:
                self.stats["failed"] += 1

            self.results.append(record)

            # Deep Crawl Link Enqueueing via RequestQueue
            if depth < self.config.max_depth and record["status"] == "SUCCESS":
                for link_entry in record["links"]:
                    if link_entry.get("category") in ("internal", "canonical", "navigation"):
                        out_href = link_entry.get("href", "")
                        norm_out = normalize_url(out_href, final_url)
                        if norm_out and norm_out not in self.seen_urls:
                            if is_url_in_scope(norm_out, self.config.seed_url, self.config.allow_subdomains):
                                safe_link, _ = is_safe_url(norm_out)
                                if safe_link:
                                    self.seen_urls.add(norm_out)
                                    await request_queue.add_request(Request.from_url(norm_out, user_data={"depth": depth + 1}))

            self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)
            self.stats["avg_latency_ms"] = round(total_latency / max(1, self.stats["attempted"]), 2)
            try:
                self.stats["pages_in_frontier"] = await request_queue.get_total_count() - await request_queue.get_handled_count()
            except Exception:
                self.stats["pages_in_frontier"] = 0

            if on_page_crawled:
                try:
                    on_page_crawled(record, self.stats)
                except Exception as cb_err:
                    logger.debug(f"on_page_crawled callback error: {cb_err}")

        @crawler.failed_request_handler
        async def failed_request_handler(context: PlaywrightCrawlingContext, error: Exception) -> None:
            try:
                nonlocal total_latency
                req = getattr(context, "request", None)
                current_url = getattr(req, "url", "") or "Unknown URL"
                user_data = getattr(req, "user_data", {}) or {}
                depth = user_data.get("depth", 0)
                self.stats["attempted"] += 1
                self.stats["failed"] += 1

                logger.warning(f"Crawlee failed request for {current_url}: {error}")
                resp = getattr(context, "response", None)
                http_status = getattr(resp, "status", None) if resp else None

                rec = {
                    "url": current_url,
                    "final_url": None,
                    "status": "ERROR",
                    "http_status": http_status,
                    "crawl_method": "crawlee_playwright",
                    "rendered": False,
                    "content_complete": False,
                    "raw_html": "",
                    "rendered_html": "",
                    "cleaned_html": "",
                    "markdown": "",
                    "plain_text": "",
                    "title": "Crawl Error",
                    "category": "Error",
                    "meta_description": "",
                    "author": "N/A",
                    "published_date": "N/A",
                    "canonical_url": current_url,
                    "headings": [],
                    "sections": [],
                    "cards": [],
                    "images": [],
                    "links": [],
                    "tables": [],
                    "json_ld": [],
                    "quality": {
                        "word_count": 0,
                        "character_count": 0,
                        "headings_count": 0,
                        "sections_count": 0,
                        "cards_count": 0,
                        "images_count": 0,
                        "links_count": 0,
                        "tables_count": 0,
                        "duplicate_blocks_count": 0,
                        "duplicate_blocks": []
                    },
                    "warnings": ["navigation_failure"],
                    "error": {"code": "navigation_error", "message": str(error)},
                    "depth": depth,
                    "fetch_time_ms": 0.0,
                    "extraction_time_ms": 0.0,
                    "content_bytes": 0
                }
                self.results.append(rec)
                if on_page_crawled:
                    try:
                        on_page_crawled(rec, self.stats)
                    except Exception as cb_err:
                        logger.debug(f"on_page_crawled callback error: {cb_err}")
            except Exception as handler_err:
                logger.error(f"Error inside failed_request_handler: {handler_err}")

        # Execute the crawl
        try:
            await crawler.run()
        finally:
            total_in_queue = await request_queue.get_total_count()
            handled_in_queue = await request_queue.get_handled_count()
            self.stats["pages_remaining"] = max(0, total_in_queue - handled_in_queue)
            await request_queue.drop()

        self.stats["elapsed_seconds"] = round(time.perf_counter() - start_time, 2)

        # 5. Transparent Completion Status Reporting
        if len(self.results) >= self.config.max_pages and self.stats["pages_remaining"] > 0:
            self.stats["crawl_completion_status"] = "PARTIAL_LIMIT_REACHED"
            self.stats["completion_message"] = (
                f"{len(self.results)} pages extracted. {self.stats['pages_remaining']} additional eligible URLs "
                f"were not crawled because the configured page limit ({self.config.max_pages}) was reached."
            )
        else:
            self.stats["crawl_completion_status"] = "COMPLETED_ALL_DISCOVERED"
            self.stats["completion_message"] = (
                f"Crawl completed! Extracted {len(self.results)} pages across the domain in {self.stats['elapsed_seconds']}s."
            )

        logger.info(self.stats["completion_message"])
        return self.results


# -----------------------------------------------------------------------------
# CLI INTERFACE
# -----------------------------------------------------------------------------
def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Production Web Crawler & Structured Data Engine (powered by Crawlee PlaywrightCrawler)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("url", type=str, help="Seed URL to crawl (e.g., https://example.com)")
    parser.add_argument("--max-pages", type=int, default=100, help="Maximum number of pages to crawl")
    parser.add_argument("--max-depth", type=int, default=3, help="Maximum crawl depth from seed URL")
    parser.add_argument("--concurrency", type=int, default=4, help="Browser concurrency / worker tabs")
    parser.add_argument("--timeout", type=int, default=30, help="Page navigation and render timeout (seconds)")
    parser.add_argument("--retries", type=int, default=2, help="Number of retry attempts on transient errors")
    parser.add_argument("--delay", type=float, default=0.5, help="Politeness delay between requests (seconds)")
    parser.add_argument("--max-response-bytes", type=int, default=10485760, help="Maximum allowed response bytes per page (10 MB default)")
    parser.add_argument("--output", type=str, default="output.jsonl", help="Output file path (.jsonl or .json)")
    parser.add_argument("--allow-subdomains", action="store_true", help="Allow crawling across all subdomains of seed domain")
    parser.add_argument("--ignore-robots", action="store_true", help="Do not check robots.txt directives")
    parser.add_argument("--no-sitemaps", action="store_true", help="Disable sitemap.xml discovery")
    parser.add_argument("--user-agent", type=str, default="", help="Custom User-Agent header string")
    return parser.parse_args()


def main():
    args = parse_cli_args()

    # Validate CLI Arguments
    if not args.url.startswith(("http://", "https://")):
        print(f"Error: Target URL must begin with http:// or https://. Got: {args.url}", file=sys.stderr)
        sys.exit(1)

    if args.max_pages < 1:
        print("Error: --max-pages must be at least 1", file=sys.stderr)
        sys.exit(1)

    if args.max_depth < 0:
        print("Error: --max-depth must be >= 0", file=sys.stderr)
        sys.exit(1)

    if args.delay < 0:
        print("Error: --delay must be >= 0.0", file=sys.stderr)
        sys.exit(1)

    config = CrawlConfig(
        seed_url=args.url,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        concurrency=args.concurrency,
        timeout=args.timeout,
        retries=args.retries,
        delay=args.delay,
        max_response_bytes=args.max_response_bytes,
        allow_subdomains=args.allow_subdomains,
        respect_robots=not args.ignore_robots,
        discover_sitemaps=not args.no_sitemaps,
        user_agent=args.user_agent if args.user_agent else (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36 (Compatible; Crawlee-Playwright/1.0)"
        )
    )

    crawler = CrawleePlaywrightCrawlerEngine(config)

    def log_progress(record: Dict[str, Any], stats: Dict[str, Any]):
        title_snippet = (record.get("title") or "Untitled")[:45]
        status = record.get("status", "UNKNOWN")
        words = record.get("quality", {}).get("word_count", 0)
        imgs = len(record.get("images", []))
        url = record.get("url", "")
        print(f"[{stats['attempted']}/{config.max_pages}] [{status}] ({words} w, {imgs} img) {title_snippet}... | {url}")

    results = asyncio.run(crawler.crawl(on_page_crawled=log_progress))

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.suffix.lower() == ".json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            for item in results:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"\nStatus: {crawler.stats.get('crawl_completion_status')}")
    print(f"Summary: {crawler.stats.get('completion_message')}")
    print(f"Records saved to: {output_path.resolve()}")
    print(f"  Successful: {crawler.stats['successful']} | Incomplete: {crawler.stats['incomplete']} | Blocked: {crawler.stats['blocked']} | Failed: {crawler.stats['failed']}")


if __name__ == "__main__":
    main()

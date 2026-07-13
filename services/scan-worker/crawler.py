from __future__ import annotations

from collections import deque
import hashlib
import json
import re
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from database import assert_safe_target_url


USER_AGENT = "OpenAuditBot/1.0 (+https://github.com/openaudit)"


def crawl_site(website: dict[str, Any], max_depth: int = 3) -> list[dict[str, Any]]:
    base_url = normalize_url(str(website["base_url"]))
    host = canonical_host(base_url)
    max_pages = max(1, min(int(website.get("max_pages") or 100), 10000))
    exclusions = exclusion_list(str(website.get("exclude_paths") or ""))
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5"})

    sitemap_urls = discover_sitemap_urls(session, base_url, host, max_pages, exclusions)
    queue = deque([(base_url, 0, "homepage")])
    queued = {base_url}
    for url in sitemap_urls:
        if url not in queued:
            queue.append((url, 1, "sitemap"))
            queued.add(url)

    pages: list[dict[str, Any]] = []
    visited: set[str] = set()
    while queue and len(pages) < max_pages:
        url, depth, source = queue.popleft()
        if url in visited or excluded(url, exclusions):
            continue
        visited.add(url)
        page = fetch_page(session, url, depth, source)
        pages.append(page)
        if depth >= max_depth or page["status_code"] >= 400 or "html" not in page["content_type"].lower():
            continue
        for link in page.pop("links", []):
            normalized = normalize_url(urljoin(url, link))
            if canonical_host(normalized) != host or normalized in queued or normalized in visited or excluded(normalized, exclusions):
                continue
            queue.append((normalized, depth + 1, "internal-link"))
            queued.add(normalized)
    for page in pages:
        page.pop("links", None)
    return pages


def discover_sitemap_urls(session: requests.Session, base_url: str, host: str, limit: int, exclusions: list[str]) -> list[str]:
    candidates = [urljoin(base_url, "/sitemap.xml")]
    discovered: list[str] = []
    checked: set[str] = set()
    while candidates and len(discovered) < limit:
        sitemap = candidates.pop(0)
        if sitemap in checked:
            continue
        checked.add(sitemap)
        try:
            response = safe_get(session, sitemap, timeout=20)
            if response.status_code >= 400 or "xml" not in response.headers.get("content-type", "").lower():
                continue
            soup = BeautifulSoup(response.text, "xml")
            locations = [node.get_text(strip=True) for node in soup.find_all("loc")]
            if soup.find("sitemapindex"):
                candidates.extend(locations[:20])
                continue
            for location in locations:
                url = normalize_url(location)
                if canonical_host(url) == host and not excluded(url, exclusions) and url not in discovered:
                    discovered.append(url)
                    if len(discovered) >= limit:
                        break
        except requests.RequestException:
            continue
    return discovered


def fetch_page(session: requests.Session, url: str, depth: int, source: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "url": url, "title": "", "meta_description": "", "language": "",
        "word_count": 0, "h1_count": 0, "content_hash": "", "content_simhash": "",
        "technical_data": {},
        "status_code": 0, "depth": depth,
        "source": source, "content_type": "", "error": "", "links": [],
    }
    try:
        response = safe_get(session, url, timeout=25)
        result["url"] = normalize_url(response.url)
        result["status_code"] = response.status_code
        result["content_type"] = response.headers.get("content-type", "")
        if "html" in result["content_type"].lower():
            soup = BeautifulSoup(response.text, "html.parser")
            result["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
            description = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
            result["meta_description"] = str(description.get("content") or "").strip() if description else ""
            result["language"] = str(soup.html.get("lang") or "").strip() if soup.html else ""
            result["h1_count"] = len(soup.find_all("h1"))
            result["technical_data"] = extract_technical_data(soup, response)
            for node in soup(["script", "style", "noscript", "template", "svg"]):
                node.decompose()
            content_root = soup.find("main") or soup.body or soup
            visible_text = content_root.get_text(" ", strip=True)
            result["word_count"] = len(re.findall(r"\b[\w'-]+\b", visible_text, flags=re.UNICODE))
            result["content_hash"], result["content_simhash"] = content_fingerprints(visible_text)
            result["links"] = [str(anchor.get("href") or "") for anchor in soup.select("a[href]")]
    except requests.RequestException as exc:
        result["error"] = str(exc)
    return result


def extract_technical_data(soup: BeautifulSoup, response: requests.Response) -> dict[str, Any]:
    canonical_nodes = soup.select('link[rel~="canonical"]')
    canonical = ""
    if canonical_nodes and canonical_nodes[0].get("href"):
        canonical = normalize_url(urljoin(response.url, str(canonical_nodes[0].get("href"))))
    robots_node = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    robots = str(robots_node.get("content") or "").lower().strip() if robots_node else ""
    x_robots = str(response.headers.get("x-robots-tag") or "").lower().strip()
    directives = ", ".join(value for value in (robots, x_robots) if value)
    og_title = meta_property_value(soup, "og:title")
    og_description = meta_property_value(soup, "og:description")

    hreflang = []
    invalid_hreflang = 0
    for node in soup.select('link[rel~="alternate"][hreflang]'):
        language = str(node.get("hreflang") or "").strip()
        href = str(node.get("href") or "").strip()
        if not language or not href:
            invalid_hreflang += 1
            continue
        hreflang.append({"language": language, "url": normalize_url(urljoin(response.url, href))})

    schema_types: set[str] = set()
    invalid_json_ld = 0
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            collect_schema_types(json.loads(node.get_text(strip=True) or "{}"), schema_types)
        except (json.JSONDecodeError, TypeError):
            invalid_json_ld += 1

    security_headers = {
        "strict_transport_security": bool(response.headers.get("strict-transport-security")),
        "content_security_policy": bool(response.headers.get("content-security-policy")),
        "x_content_type_options": str(response.headers.get("x-content-type-options") or "").lower() == "nosniff",
        "referrer_policy": bool(response.headers.get("referrer-policy")),
        "permissions_policy": bool(response.headers.get("permissions-policy")),
    }
    return {
        "canonical": canonical,
        "canonical_count": len(canonical_nodes),
        "canonical_cross_domain": bool(canonical and canonical_host(canonical) != canonical_host(response.url)),
        "robots": directives,
        "indexable": "noindex" not in directives,
        "open_graph": {"title": og_title, "description": og_description},
        "hreflang": hreflang,
        "invalid_hreflang": invalid_hreflang,
        "structured_data_types": sorted(schema_types),
        "invalid_json_ld": invalid_json_ld,
        "security_headers": security_headers,
    }


def meta_property_value(soup: BeautifulSoup, property_name: str) -> str:
    node = soup.find("meta", attrs={"property": re.compile(f"^{re.escape(property_name)}$", re.I)})
    return str(node.get("content") or "").strip() if node else ""


def collect_schema_types(value: Any, types: set[str]) -> None:
    if isinstance(value, dict):
        schema_type = value.get("@type")
        if isinstance(schema_type, str):
            types.add(schema_type)
        elif isinstance(schema_type, list):
            types.update(str(item) for item in schema_type if item)
        for child in value.values():
            collect_schema_types(child, types)
    elif isinstance(value, list):
        for child in value:
            collect_schema_types(child, types)


def content_fingerprints(value: str) -> tuple[str, str]:
    words = [
        token.lower()
        for token in re.findall(r"\b[\w'-]+\b", str(value or ""), flags=re.UNICODE)
        if len(token) > 1
    ]
    if len(words) < 40:
        return "", ""
    normalized = " ".join(words)
    exact_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    shingles = [" ".join(words[index:index + 4]) for index in range(max(1, len(words) - 3))]
    vector = [0] * 64
    for shingle in shingles:
        hashed = int.from_bytes(hashlib.sha256(shingle.encode("utf-8")).digest()[:8], "big")
        for bit in range(64):
            vector[bit] += 1 if hashed & (1 << bit) else -1
    simhash = 0
    for bit, weight in enumerate(vector):
        if weight >= 0:
            simhash |= 1 << bit
    return exact_hash, f"{simhash:016x}"


def normalize_url(value: str) -> str:
    value, _ = urldefrag(value.strip())
    parsed = urlparse(value)
    path = parsed.path or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def canonical_host(value: str) -> str:
    return urlparse(value).netloc.lower().removeprefix("www.").split(":", 1)[0]


def exclusion_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def excluded(url: str, exclusions: list[str]) -> bool:
    path = urlparse(url).path
    return any(path.startswith(item) or item in url for item in exclusions)


def safe_get(session: requests.Session, url: str, timeout: int) -> requests.Response:
    current = url
    for _ in range(6):
        assert_safe_target_url(current, resolve_dns=True)
        response = session.get(current, timeout=timeout, allow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        current = normalize_url(urljoin(current, location))
    raise requests.RequestException("Too many redirects while crawling target.")

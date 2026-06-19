from __future__ import annotations

from collections import deque
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
    result: dict[str, Any] = {"url": url, "title": "", "status_code": 0, "depth": depth, "source": source, "content_type": "", "error": "", "links": []}
    try:
        response = safe_get(session, url, timeout=25)
        result["url"] = normalize_url(response.url)
        result["status_code"] = response.status_code
        result["content_type"] = response.headers.get("content-type", "")
        if "html" in result["content_type"].lower():
            soup = BeautifulSoup(response.text, "html.parser")
            result["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
            result["links"] = [str(anchor.get("href") or "") for anchor in soup.select("a[href]")]
    except requests.RequestException as exc:
        result["error"] = str(exc)
    return result


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

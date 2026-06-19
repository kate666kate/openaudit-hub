from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
URLS_FILE = ROOT / "config" / "lhci" / "urls.txt"
REPORTS_DIR = ROOT / "outputs" / "reports"
USER_AGENT = "OpenAuditSitemapCrawler/1.0"


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch XML sitemaps for OpenAudit sites.")
    parser.add_argument("--url", action="append", default=[], help="Site URL to crawl. Can be used multiple times.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum URLs to keep per site.")
    parser.add_argument("--output-dir", default=str(REPORTS_DIR), help="Directory for JSON reports.")
    args = parser.parse_args()

    targets = args.url or load_targets()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not targets:
        print("No targets found. Add URLs to config/lhci/urls.txt or pass --url.")
        return 1

    for target in targets:
        report = crawl_site(target, args.limit)
        filename = f"sitemap-{site_key(target)}-{timestamp_slug()}.json"
        output_path = output_dir / filename
        output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"{target} -> {len(report['urls'])} URL(s), {len(report['sitemaps'])} sitemap(s), {len(report['errors'])} error(s)")
        print(f"  {output_path}")

    return 0


def load_targets() -> list[str]:
    if not URLS_FILE.exists():
        return []
    return [
        line.strip()
        for line in URLS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def crawl_site(target: str, limit: int) -> dict[str, Any]:
    root = normalize_site_url(target)
    queue = [urljoin(root, "/sitemap.xml")]
    seen_sitemaps: set[str] = set()
    urls: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    errors: list[dict[str, str]] = []

    while queue and len(urls) < limit:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)

        try:
            body = fetch_text(sitemap_url)
            parsed = parse_sitemap(body)
        except Exception as exc:  # noqa: BLE001 - report all crawler failures to UI.
            errors.append({"url": sitemap_url, "error": str(exc)})
            continue

        for nested in parsed["sitemaps"]:
            if same_host(root, nested) and nested not in seen_sitemaps and nested not in queue:
                queue.append(nested)

        for item in parsed["urls"]:
            loc = item.get("loc", "")
            if not loc or loc in seen_urls or not same_host(root, loc):
                continue
            seen_urls.add(loc)
            urls.append(item)
            if len(urls) >= limit:
                break

    return {
        "kind": "openaudit-sitemap-crawl",
        "site": root,
        "site_key": site_key(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sitemap_entry": urljoin(root, "/sitemap.xml"),
        "sitemaps": sorted(seen_sitemaps),
        "urls": urls,
        "url_count": len(urls),
        "sitemap_count": len(seen_sitemaps),
        "error_count": len(errors),
        "errors": errors,
        "limit": limit,
    }


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=20) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def parse_sitemap(xml_text: str) -> dict[str, list[Any]]:
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    tag = strip_namespace(root.tag)
    result: dict[str, list[Any]] = {"sitemaps": [], "urls": []}

    if tag == "sitemapindex":
        for node in root:
            if strip_namespace(node.tag) != "sitemap":
                continue
            loc = child_text(node, "loc")
            if loc:
                result["sitemaps"].append(loc)
        return result

    if tag == "urlset":
        for node in root:
            if strip_namespace(node.tag) != "url":
                continue
            loc = child_text(node, "loc")
            if not loc:
                continue
            result["urls"].append(
                {
                    "loc": loc,
                    "lastmod": child_text(node, "lastmod"),
                    "changefreq": child_text(node, "changefreq"),
                    "priority": child_text(node, "priority"),
                }
            )
        return result

    raise RuntimeError(f"Unsupported sitemap root: {tag}")


def child_text(node: ElementTree.Element, child_name: str) -> str:
    for child in node:
        if strip_namespace(child.tag) == child_name:
            return (child.text or "").strip()
    return ""


def strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_site_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    scheme = parsed.scheme or "https"
    host = parsed.netloc or parsed.path
    return f"{scheme}://{host.strip('/')}/"


def same_host(root: str, url: str) -> bool:
    return urlparse(root).netloc.lower() == urlparse(url).netloc.lower()


def site_key(url: str) -> str:
    host = urlparse(normalize_site_url(url)).netloc.lower()
    return re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "site"


def timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


if __name__ == "__main__":
    raise SystemExit(main())

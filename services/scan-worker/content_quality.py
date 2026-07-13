from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def extract_content_issues(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    html_pages = [page for page in pages if int(page.get("status_code") or 0) < 400 and "html" in str(page.get("content_type") or "").lower()]
    rows: list[dict[str, Any]] = []
    titles: dict[str, list[dict[str, Any]]] = {}
    descriptions: dict[str, list[dict[str, Any]]] = {}

    for page in html_pages:
        title = str(page.get("title") or "").strip()
        description = str(page.get("meta_description") or "").strip()
        language = str(page.get("language") or "").strip()
        word_count = int(page.get("word_count") or 0)
        h1_count = int(page.get("h1_count") or 0)
        if title:
            titles.setdefault(normalize_content_value(title), []).append(page)
            if len(title) < 20 or len(title) > 65:
                rows.append(content_issue("content-title-length", "Page title length needs review", page, "head > title", title, f"Title contains {len(title)} characters; aim for 20-65.", 1.0, "Medium"))
        else:
            rows.append(content_issue("content-missing-title", "Page is missing a title", page, "head", "", "Add a unique, descriptive HTML title.", 3.0, "High"))
        if description:
            descriptions.setdefault(normalize_content_value(description), []).append(page)
            if len(description) < 70 or len(description) > 160:
                rows.append(content_issue("content-meta-length", "Meta description length needs review", page, 'meta[name="description"]', description, f"Description contains {len(description)} characters; aim for 70-160.", 1.0, "Medium"))
        else:
            rows.append(content_issue("content-missing-meta-description", "Page is missing a meta description", page, "head", "", "Add a page-specific summary for search results and sharing.", 2.0, "Medium"))
        if h1_count == 0:
            rows.append(content_issue("content-missing-h1", "Page has no H1 heading", page, "body", "", "Add one clear H1 that describes the page purpose.", 2.0, "Medium"))
        elif h1_count > 1:
            rows.append(content_issue("content-multiple-h1", "Page has multiple H1 headings", page, "h1", f"{h1_count} H1 elements", "Use one primary H1 and demote section headings where appropriate.", 1.0, "Medium"))
        if word_count < 150:
            rows.append(content_issue("content-thin-page", "Page may have thin content", page, "main", f"{word_count} words", "Review whether the page fully answers its intended user need; aim for at least 150 meaningful words when appropriate.", 1.5, "Medium"))
        if not language:
            rows.append(content_issue("content-missing-language", "Page language is not declared", page, "html", "<html>", "Set a valid lang attribute such as en-AU on the html element.", 1.0, "Low"))

        rows.extend(extract_technical_issues(page))

    rows.extend(duplicate_content_issues("content-duplicate-title", "Duplicate page title", titles, "head > title", 2.0))
    rows.extend(duplicate_content_issues("content-duplicate-meta-description", "Duplicate meta description", descriptions, 'meta[name="description"]', 1.5))
    return rows


def write_content_report(output_dir: Path, website_key: str, pages: list[dict[str, Any]], issues: list[dict[str, Any]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(":", "-").replace(".", "-")
    output_path = output_dir / f"content-{website_key}-{stamp}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "website_key": website_key,
        "page_count": len(pages),
        "issue_count": len(issues),
        "pages": [{key: page.get(key) for key in ("url", "title", "meta_description", "language", "word_count", "h1_count", "status_code", "technical_data")} for page in pages],
        "issues": issues,
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def content_issue(issue_id: str, title: str, page: dict[str, Any], selector: str, snippet: str, explanation: str, points: float, difficulty: str, category: str = "Content Quality", responsibility: str = "Content / SEO") -> dict[str, Any]:
    return {
        "id": issue_id, "source": "content", "title": title, "category": category,
        "difficulty": difficulty, "responsibility": responsibility, "occurrences": 1,
        "points": points, "page_url": str(page.get("url") or ""),
        "affected_examples": [{"page_url": str(page.get("url") or ""), "selector": selector, "snippet": snippet, "explanation": explanation}],
    }


def extract_technical_issues(page: dict[str, Any]) -> list[dict[str, Any]]:
    data = page.get("technical_data") if isinstance(page.get("technical_data"), dict) else {}
    if not data:
        return []
    rows: list[dict[str, Any]] = []
    indexable = bool(data.get("indexable", True))
    canonical = str(data.get("canonical") or "")
    canonical_count = int(data.get("canonical_count") or 0)
    technical = {"category": "Technical SEO", "responsibility": "Development / SEO"}
    security = {"category": "Security", "responsibility": "Development / Security"}

    if indexable and not canonical:
        rows.append(content_issue("technical-missing-canonical", "Indexable page has no canonical URL", page, "head", "", "Add one self-referencing canonical link, or point it to the preferred equivalent page.", 1.5, "Medium", **technical))
    if canonical_count > 1:
        rows.append(content_issue("technical-multiple-canonical", "Page declares multiple canonical URLs", page, 'link[rel="canonical"]', f"{canonical_count} canonical links", "Keep exactly one canonical declaration so search engines receive a clear signal.", 2.0, "High", **technical))
    if data.get("canonical_cross_domain"):
        rows.append(content_issue("technical-cross-domain-canonical", "Canonical points to another domain", page, 'link[rel="canonical"]', canonical, "Confirm this cross-domain canonical is intentional; otherwise replace it with the preferred URL on this site.", 1.5, "Medium", **technical))
    if str(page.get("source") or "") == "sitemap" and not indexable:
        rows.append(content_issue("technical-sitemap-noindex", "Sitemap page is marked noindex", page, 'meta[name="robots"]', str(data.get("robots") or "noindex"), "Remove the URL from the sitemap or remove noindex when the page should appear in search.", 3.0, "High", **technical))

    open_graph = data.get("open_graph") if isinstance(data.get("open_graph"), dict) else {}
    if not open_graph.get("title") or not open_graph.get("description"):
        rows.append(content_issue("technical-open-graph", "Social sharing metadata is incomplete", page, "head", "", "Add page-specific og:title and og:description values so shared links have a useful preview.", 0.5, "Low", **technical))
    if int(data.get("invalid_hreflang") or 0):
        rows.append(content_issue("technical-invalid-hreflang", "Hreflang declaration is incomplete", page, 'link[rel="alternate"][hreflang]', f"{data.get('invalid_hreflang')} invalid declaration(s)", "Give every hreflang declaration both a valid language code and an absolute destination URL.", 1.5, "Medium", **technical))
    if int(data.get("invalid_json_ld") or 0):
        rows.append(content_issue("technical-invalid-json-ld", "Structured data contains invalid JSON", page, 'script[type="application/ld+json"]', f"{data.get('invalid_json_ld')} invalid block(s)", "Correct the JSON syntax, then validate the block with a Schema.org or rich-results validator.", 2.0, "High", **technical))

    headers = data.get("security_headers") if isinstance(data.get("security_headers"), dict) else {}
    header_checks = [
        ("content_security_policy", "security-missing-csp", "Content-Security-Policy header is missing", "Define a tested Content-Security-Policy to restrict unexpected script and resource execution.", 2.0, "High"),
        ("x_content_type_options", "security-missing-nosniff", "X-Content-Type-Options header is missing", "Return X-Content-Type-Options: nosniff on HTML and asset responses.", 1.0, "Medium"),
        ("referrer_policy", "security-missing-referrer-policy", "Referrer-Policy header is missing", "Set a deliberate Referrer-Policy such as strict-origin-when-cross-origin.", 0.5, "Low"),
    ]
    if str(page.get("url") or "").lower().startswith("https://"):
        header_checks.insert(0, ("strict_transport_security", "security-missing-hsts", "Strict-Transport-Security header is missing", "Enable HSTS after confirming the site and its required subdomains are HTTPS-ready.", 1.5, "Medium"))
    for field, issue_id, title, action, points, difficulty in header_checks:
        if headers and not headers.get(field):
            rows.append(content_issue(issue_id, title, page, "response headers", field, action, points, difficulty, **security))
    return rows


def duplicate_content_issues(issue_id: str, title: str, grouped: dict[str, list[dict[str, Any]]], selector: str, points: float) -> list[dict[str, Any]]:
    rows = []
    for value, pages in grouped.items():
        if not value or len(pages) < 2:
            continue
        for page in pages:
            rows.append(content_issue(issue_id, title, page, selector, value, f"This value is shared by {len(pages)} pages. Make it unique to this page.", points, "High"))
    return rows


def normalize_content_value(value: str) -> str:
    return " ".join(value.lower().split())

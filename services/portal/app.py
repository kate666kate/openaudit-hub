from __future__ import annotations

import os
import json
import re
import csv
import io
from collections import Counter
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.parse import quote
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory

try:
    from .database import (
        create_scan_job,
        create_website,
        database_is_ready,
        delete_website,
        get_scan_job,
        get_website,
        init_database,
        list_issues as database_issues,
        list_crawl_pages,
        list_scan_jobs,
        list_websites,
        reconcile_issues,
        update_issue_status,
        update_scan_job,
        update_website,
    )
except ImportError:
    from database import (
        create_scan_job,
        create_website,
        database_is_ready,
        delete_website,
        get_scan_job,
        get_website,
        init_database,
        list_issues as database_issues,
        list_crawl_pages,
        list_scan_jobs,
        list_websites,
        reconcile_issues,
        update_issue_status,
        update_scan_job,
        update_website,
    )

try:
    from .scan_queue import enqueue_scan
except ImportError:
    from scan_queue import enqueue_scan

try:
    import yake
except ImportError:  # pragma: no cover - optional until the Docker image is rebuilt.
    yake = None


BASE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = BASE_DIR.parents[1] if len(BASE_DIR.parents) > 1 else BASE_DIR
REPORTS_DIR = (
    BASE_DIR / "reports"
    if (BASE_DIR / "reports").exists()
    else WORKSPACE_DIR / "outputs" / "reports"
)
URLS_FILE = (
    BASE_DIR / "config" / "lhci" / "urls.txt"
    if (BASE_DIR / "config" / "lhci" / "urls.txt").exists()
    else WORKSPACE_DIR / "config" / "lhci" / "urls.txt"
)
SEARCH_CONSOLE_DIRS = [
    WORKSPACE_DIR / "config" / "search-console",
    WORKSPACE_DIR / "outputs" / "search-console",
]
_LIGHTHOUSE_SUMMARY_CACHE: dict[str, Any] = {}


def create_app() -> Flask:
    init_database(URLS_FILE)
    app = Flask(__name__)

    @app.get("/health/live")
    def health_live():
        return jsonify({"status": "ok", "service": "openaudit-portal"})

    @app.get("/health/ready")
    def health_ready():
        ready = database_is_ready()
        return jsonify({"status": "ready" if ready else "unavailable", "database": ready}), 200 if ready else 503

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            **portal_template_context(active_slug="dashboard"),
        )

    @app.route("/modules/<slug>")
    def module_page(slug: str):
        page = find_module_page(slug)
        if not page:
            abort(404)
        matomo = load_matomo_summary() if is_analytics_page(slug) else None
        marketing = load_marketing_summary() if slug == "email-marketing" else None
        publishing = load_publishing_summary() if slug == "drupal-sdp-publishing" else None
        return render_template(
            "module.html",
            page=page,
            matomo=matomo,
            marketing=marketing,
            publishing=publishing,
            **portal_template_context(active_slug=slug),
        )

    @app.route("/api/reports")
    def reports():
        site = resolve_requested_site_url()
        return jsonify(load_reports(site))

    @app.route("/api/targets")
    def targets():
        return jsonify(load_targets())

    @app.route("/websites", methods=["GET", "POST"])
    def website_management():
        error = ""
        if request.method == "POST":
            try:
                website = create_website(
                    {
                        "name": request.form.get("name", ""),
                        "base_url": request.form.get("base_url", ""),
                        "schedule": request.form.get("schedule", "manual"),
                        "max_pages": request.form.get("max_pages", "100"),
                        "exclude_paths": request.form.get("exclude_paths", ""),
                    }
                )
                return redirect(f"/websites?created={quote(website['key'], safe='')}")
            except (ValueError, TypeError) as exc:
                error = str(exc)
        return render_template(
            "websites.html",
            websites=list_websites(),
            error=error,
            created=request.args.get("created", ""),
            **portal_template_context(active_slug="websites"),
        )

    @app.route("/websites/<key>", methods=["POST"])
    def website_update_form(key: str):
        action = request.form.get("action", "update")
        if action == "delete":
            delete_website(key)
            return redirect("/websites")
        update_website(
            key,
            {
                "name": request.form.get("name", ""),
                "active": request.form.get("active") == "on",
                "schedule": request.form.get("schedule", "manual"),
                "max_pages": request.form.get("max_pages", "100"),
                "exclude_paths": request.form.get("exclude_paths", ""),
            },
        )
        return redirect(f"/websites?updated={quote(key, safe='')}")

    @app.route("/api/websites", methods=["GET", "POST"])
    def websites_api():
        if request.method == "POST":
            try:
                return jsonify(create_website(request.get_json(silent=True) or {})), 201
            except (ValueError, TypeError) as exc:
                return jsonify({"error": str(exc)}), 400
        return jsonify(list_websites())

    @app.route("/api/websites/<key>", methods=["GET", "PATCH", "DELETE"])
    def website_api(key: str):
        if request.method == "DELETE":
            return ("", 204) if delete_website(key) else (jsonify({"error": "Website not found."}), 404)
        if request.method == "PATCH":
            try:
                website = update_website(key, request.get_json(silent=True) or {})
            except (ValueError, TypeError) as exc:
                return jsonify({"error": str(exc)}), 400
            return jsonify(website) if website else (jsonify({"error": "Website not found."}), 404)
        website = get_website(key)
        return jsonify(website) if website else (jsonify({"error": "Website not found."}), 404)

    @app.route("/scans", methods=["GET", "POST"])
    def scans_page():
        queue_error = ""
        if request.method == "POST":
            website_key_value = request.form.get("website_key", "").strip()
            scan_type = request.form.get("scan_type", "full").strip()
            job = None
            try:
                job = create_scan_job(website_key_value, scan_type)
                task_id = enqueue_scan(str(job["id"]))
                update_scan_job(str(job["id"]), task_id=task_id, message="Waiting for a scan worker.")
                return redirect(f"/scans?job={quote(str(job['id']), safe='')}")
            except Exception as exc:
                queue_error = str(exc)
                if job:
                    update_scan_job(str(job["id"]), status="failed", progress=100, message=queue_error, finished_at=datetime.now(timezone.utc))
        selected_key = request.args.get("site", "").strip()
        return render_template(
            "scans.html",
            jobs=list_scan_jobs(selected_key or None),
            websites=list_websites(active_only=True),
            queue_error=queue_error,
            selected_job=request.args.get("job", ""),
            **portal_template_context(active_slug="scans"),
        )

    @app.route("/api/scans", methods=["GET", "POST"])
    def scans_api():
        if request.method == "GET":
            return jsonify(list_scan_jobs(request.args.get("site", "").strip() or None))
        payload = request.get_json(silent=True) or {}
        job = None
        try:
            job = create_scan_job(str(payload.get("website_key") or ""), str(payload.get("scan_type") or "full"))
            task_id = enqueue_scan(str(job["id"]))
            job = update_scan_job(str(job["id"]), task_id=task_id, message="Waiting for a scan worker.") or job
            return jsonify(job), 202
        except Exception as exc:
            if job:
                update_scan_job(str(job["id"]), status="failed", progress=100, message=str(exc), finished_at=datetime.now(timezone.utc))
            return jsonify({"error": str(exc)}), 503 if job else 400

    @app.route("/api/scans/<job_id>")
    def scan_api(job_id: str):
        job = get_scan_job(job_id)
        return jsonify(job) if job else (jsonify({"error": "Scan job not found."}), 404)

    @app.route("/api/lifecycle/issues")
    def lifecycle_issues_api():
        site = resolve_requested_site_url()
        if not site:
            return jsonify([])
        return jsonify(database_issues(site_key(site), request.args.get("status", "").strip()))

    @app.route("/api/crawl-pages")
    def crawl_pages_api():
        site = resolve_requested_site_url()
        return jsonify(list_crawl_pages(site_key(site), int(request.args.get("limit", "500")))) if site else jsonify([])

    @app.route("/api/lifecycle/issues/<issue_id>", methods=["PATCH"])
    def lifecycle_issue_api(issue_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            issue = update_issue_status(
                issue_id,
                str(payload.get("status") or "open"),
                str(payload.get("owner") or ""),
                str(payload.get("ignored_reason") or ""),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(issue) if issue else (jsonify({"error": "Issue not found."}), 404)

    @app.route("/issues/lifecycle/<issue_id>", methods=["POST"])
    def lifecycle_issue_form(issue_id: str):
        site_key_value = request.form.get("site", "").strip()
        try:
            update_issue_status(
                issue_id,
                request.form.get("status", "open"),
                request.form.get("owner", ""),
                request.form.get("ignored_reason", ""),
            )
        except ValueError:
            pass
        target = "/modules/issues"
        if site_key_value:
            target += f"?site={quote(site_key_value, safe='')}"
        return redirect(target)

    @app.route("/api/lhci")
    def lhci():
        return jsonify(load_lhci_summary())

    @app.route("/api/lighthouse")
    def lighthouse():
        site = resolve_requested_site_url()
        return jsonify(load_lighthouse_report_summary(site))

    @app.route("/api/history")
    def history():
        site = resolve_requested_site_url()
        return jsonify(build_audit_history(site))

    @app.route("/api/history/export.csv")
    def history_export():
        site = resolve_requested_site_url()
        history = build_audit_history(site)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "website",
                "updated",
                "report",
                "url",
                "overview_score",
                "delta",
                "performance",
                "accessibility",
                "best_practices",
                "seo",
                "issue_count",
                "regressions",
            ]
        )
        for row in reversed(history.get("rows", [])):
            scores = row.get("scores", {})
            writer.writerow(
                [
                    site_label(site or ""),
                    row.get("updated", ""),
                    row.get("report", ""),
                    row.get("url", ""),
                    row.get("overview", ""),
                    row.get("delta_label", ""),
                    scores.get("Performance", ""),
                    scores.get("Accessibility", ""),
                    scores.get("Best Practices", ""),
                    scores.get("SEO", ""),
                    row.get("issue_count", ""),
                    "; ".join(
                        f"{item.get('category')} {item.get('delta')}"
                        for item in row.get("regressions", [])
                    ),
                ]
            )
        filename = f"audit-history-{site_key(site or 'site')}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/api/issues")
    def issues():
        site = resolve_requested_site_url()
        summary = load_lighthouse_report_summary(site)
        return jsonify(
            {
                "issues_to_fix": summary.get("issues_to_fix", []),
                "resolved_checks": summary.get("resolved_checks", []),
                "role_buckets": summary.get("role_buckets", []),
            }
        )

    @app.route("/api/action-plan")
    def action_plan():
        site = resolve_requested_site_url()
        summary = load_lighthouse_report_summary(site)
        return jsonify(
            {
                "website": site_label(site or str(summary.get("final_url", ""))),
                "generated_at": summary.get("report_source", {}).get("generated_at", ""),
                "tasks": summary.get("action_queue", []),
            }
        )

    @app.route("/api/standards")
    def standards():
        site = resolve_requested_site_url()
        summary = load_lighthouse_report_summary(site)
        return jsonify(
            {
                "standards_engine": summary.get("standards_engine", {}),
                "capability_matrix": summary.get("capability_matrix", []),
                "integration_summary": summary.get("integration_summary", []),
                "export_formats": summary.get("export_formats", []),
            }
        )

    @app.route("/api/modules")
    def modules():
        site = resolve_requested_site_url()
        summary = load_lighthouse_report_summary(site)
        return jsonify(
            {
                "feature_modules": summary.get("feature_modules", []),
                "seo_advanced": summary.get("seo_advanced", {}),
                "keyword_suggestions": summary.get("keyword_suggestions", {}),
                "accessibility_breakdown": summary.get("accessibility_breakdown", []),
                "prepublish_summary": summary.get("prepublish_summary", []),
                "analytics_summary": summary.get("analytics_summary", {}),
                "campaign_summary": summary.get("campaign_summary", {}),
                "behavior_summary": summary.get("behavior_summary", {}),
                "content_quality": summary.get("content_quality", {}),
                "link_integrity": summary.get("link_integrity", {}),
                "document_governance": summary.get("document_governance", {}),
                "privacy_summary": summary.get("privacy_summary", {}),
                "response_summary": summary.get("response_summary", {}),
                "connector_summary": summary.get("connector_summary", []),
                "crawler_summary": summary.get("crawler_summary", {}),
                "architecture_summary": summary.get("architecture_summary", {}),
                "comparison_summary": summary.get("comparison_summary", {}),
                "ai_recommendations": summary.get("ai_recommendations", {}),
                "matomo": load_matomo_summary(include_live=False),
                "marketing": load_marketing_summary(),
                "publishing": load_publishing_summary(),
            }
        )

    @app.route("/api/keyword-suggestions/export.csv")
    def keyword_suggestions_export():
        site = resolve_requested_site_url()
        summary = load_lighthouse_report_summary(site)
        keywords = summary.get("keyword_suggestions", {})
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "website",
                "page",
                "target_keyword",
                "intent",
                "priority",
                "points",
                "clicks",
                "impressions",
                "ctr",
                "position",
                "title_tag",
                "h1",
                "meta_description",
                "intro_copy_guidance",
                "image_alt_example",
                "internal_link_guidance",
            ]
        )
        for brief in keywords.get("optimization_briefs", []):
            writer.writerow(
                [
                    site_label(site or ""),
                    brief.get("page", ""),
                    brief.get("keyword", ""),
                    brief.get("intent", ""),
                    brief.get("priority", ""),
                    brief.get("points", ""),
                    brief.get("clicks", ""),
                    brief.get("impressions", ""),
                    brief.get("ctr", ""),
                    brief.get("position", ""),
                    brief.get("title", ""),
                    brief.get("h1", ""),
                    brief.get("meta", ""),
                    brief.get("intro", ""),
                    brief.get("alt", ""),
                    brief.get("internal_links", ""),
                ]
            )
        filename = f"keyword-suggestions-{site_key(site or 'site')}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/api/matomo")
    def matomo():
        return jsonify(load_matomo_summary())

    @app.route("/api/marketing")
    def marketing():
        return jsonify(load_marketing_summary())

    @app.route("/api/publishing")
    def publishing():
        return jsonify(load_publishing_summary())

    @app.route("/issues/<issue_id>")
    def issue_detail(issue_id: str):
        site = resolve_requested_site_url()
        summary = load_lighthouse_report_summary(site)
        issue = find_issue(summary.get("issues_to_fix", []), issue_id)
        if not issue:
            target = f"/modules/issues?site={quote(site_key(site), safe='')}" if site else "/modules/issues"
            return redirect(target)

        return render_template(
            "issue.html",
            title=os.getenv("PORTAL_TITLE", "OpenAudit Hub"),
            i…42663 tokens truncated…e of the duplicate id.",
                    "Generate a stable unique suffix from the component or record identifier.",
                    "Update for, aria-labelledby, aria-describedby, and href references.",
                    "Test all repeated components after the change.",
                ],
                "bad_example": '<input id="product-name">\n<input id="product-name">',
                "good_example": '<input id="product-name-101">\n<input id="product-name-102">',
                "verify": "Each id occurs once and all referencing attributes point to the intended element.",
            }
        )
    elif any(token in text for token in ["aria", "wai-aria"]):
        guidance.update(
            {
                "summary": "Correct the ARIA role, state, property, or reference; prefer native HTML when it provides the required behaviour.",
                "where": "Interactive component, navigation, modal, tab, accordion, or custom widget.",
                "owner": "Development",
                "steps": [
                    "Check whether a native button, input, details, nav, or dialog can replace the custom role.",
                    "Remove unsupported ARIA attributes and repair broken id references.",
                    "Keep dynamic states such as aria-expanded synchronized with visual state.",
                    "Test keyboard behaviour and the accessibility tree.",
                ],
                "bad_example": '<div role="button">Submit</div>',
                "good_example": '<button type="submit">Submit</button>',
                "verify": "The element exposes the correct role, name, state, and keyboard behaviour, and Pa11y passes the ARIA check.",
            }
        )
    elif any(token in text for token in ["landmark", "bypass", "skip link"]):
        guidance.update(
            {
                "summary": "Provide semantic landmarks and a working skip link so keyboard users can bypass repeated navigation.",
                "where": "Global page layout, header, navigation, and main content wrapper.",
                "owner": "Development",
                "steps": [
                    "Wrap the primary content in one main element with a stable id.",
                    "Add a keyboard-visible skip link as the first focusable control.",
                    "Use header, nav, main, aside, and footer landmarks consistently.",
                ],
                "bad_example": '<div class="content">...</div>',
                "good_example": '<a class="skip-link" href="#main">Skip to content</a>\n<main id="main">...</main>',
                "verify": "Keyboard users can activate the skip link and focus moves to the main content landmark.",
            }
        )
    return guidance


def difficulty_label(points: float, score: float) -> str:
    if points >= 5 or score < 0.35:
        return "High"
    if points >= 1.5 or score < 0.7:
        return "Medium"
    return "Low"


def difficulty_dots(points: float, score: float) -> str:
    if points >= 5 or score < 0.35:
        return "4"
    if points >= 1.5 or score < 0.7:
        return "3"
    return "2"


def load_lhci_summary() -> dict[str, Any]:
    client = LhciClient()
    projects = client.get_json("/v1/projects") or []

    project_summaries = []
    latest_build = None

    for project in projects:
        project_id = project.get("id")
        slug = project.get("slug") or ""
        builds = client.get_json(f"/v1/projects/{project_id}/builds?limit=5") or []
        urls = client.get_json(f"/v1/projects/{project_id}/urls") or []
        build = builds[0] if builds else None
        latest_stats = []

        if build:
            build_id = build.get("id")
            latest_stats = (
                client.get_json(f"/v1/projects/{project_id}/builds/{build_id}/statistics")
                or []
            )

        summary = {
            "name": project.get("name", "Unnamed project"),
            "slug": slug,
            "dashboard_url": f"{os.getenv('LHCI_URL', 'http://localhost:9001')}/app/projects/{slug}/dashboard"
            if slug
            else os.getenv("LHCI_URL", "http://localhost:9001"),
            "build_count": len(builds),
            "url_count": len(urls),
            "latest_build": format_build(build),
            "scores": extract_scores(latest_stats),
        }
        project_summaries.append(summary)

        if build and latest_build is None:
            latest_build = summary

    return {
        "ok": client.ok,
        "error": client.error,
        "project_count": len(project_summaries),
        "projects": project_summaries,
        "latest_project": latest_build or (project_summaries[0] if project_summaries else None),
    }


def load_seo_summary() -> dict[str, Any]:
    pages = [inspect_seo_target(target) for target in load_targets()[:12]]
    issue_count = sum(len(page["issues"]) for page in pages)
    average_score = round(sum(page["score"] for page in pages) / len(pages)) if pages else 0
    return {
        "page_count": len(pages),
        "issue_count": issue_count,
        "average_score": average_score,
        "pages": pages,
    }


def inspect_seo_target(url: str) -> dict[str, Any]:
    result = {
        "url": url,
        "status": "error",
        "score": 0,
        "title": "",
        "description": "",
        "h1_count": 0,
        "canonical": "",
        "issues": [],
        "checks": {},
    }

    try:
        response = requests.get(
            url,
            timeout=6,
            headers={"User-Agent": "OpenAuditBot/1.0"},
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        result["issues"].append(f"Request failed: {exc}")
        return result

    result["status"] = str(response.status_code)
    if response.status_code >= 400:
        result["issues"].append(f"HTTP status is {response.status_code}")

    soup = BeautifulSoup(response.text, "html.parser")
    title = normalize_text(soup.title.string if soup.title else "")
    description = meta_content(soup, "description")
    canonical = link_href(soup, "canonical")
    robots = meta_content(soup, "robots").lower()
    lang = (soup.html.get("lang", "") if soup.html else "").strip()
    h1s = [normalize_text(h.get_text(" ")) for h in soup.find_all("h1")]
    og_title = meta_property(soup, "og:title")
    og_description = meta_property(soup, "og:description")

    checks = {
        "HTTP OK": response.status_code < 400,
        "Title length": 30 <= len(title) <= 65,
        "Meta description": 70 <= len(description) <= 170,
        "Single H1": len(h1s) == 1,
        "Canonical": bool(canonical),
        "Indexable": "noindex" not in robots,
        "HTML language": bool(lang),
        "Open Graph": bool(og_title and og_description),
    }

    issues = []
    if not checks["Title length"]:
        issues.append(f"Title length is {len(title)} characters")
    if not checks["Meta description"]:
        issues.append(f"Meta description length is {len(description)} characters")
    if not checks["Single H1"]:
        issues.append(f"H1 count is {len(h1s)}")
    if not checks["Canonical"]:
        issues.append("Canonical link is missing")
    if not checks["Indexable"]:
        issues.append("Robots meta includes noindex")
    if not checks["HTML language"]:
        issues.append("HTML lang attribute is missing")
    if not checks["Open Graph"]:
        issues.append("Open Graph title or description is missing")

    passed = sum(1 for passed_check in checks.values() if passed_check)
    result.update(
        {
            "status": str(response.status_code),
            "score": round((passed / len(checks)) * 100),
            "title": title or "Untitled",
            "description": description,
            "h1_count": len(h1s),
            "canonical": urljoin(response.url, canonical) if canonical else "",
            "issues": result["issues"] + issues,
            "checks": checks,
        }
    )
    return result


def load_service_statuses() -> list[dict[str, str]]:
    lighthouse = load_lighthouse_report_summary()
    reports = load_reports()
    targets = load_targets()
    modules = [
        {
            "name": "DCI Score",
            "status": "active" if lighthouse.get("ok") else "setup needed",
            "detail": "Composite quality score across accessibility, SEO, performance, and best practices",
            "metric": lighthouse.get("overview_score", "n/a"),
            "action": "Open overview",
            "href": "#overview",
        },
        {
            "name": "Quality Assurance",
            "status": "active" if lighthouse.get("ok") else "setup needed",
            "detail": f"{lighthouse.get('issue_count', 0)} open issues across {len(targets)} tracked URL(s)",
            "metric": lighthouse.get("issue_count", "0"),
            "action": "View pages",
            "href": "#page",
        },
        {
            "name": "Accessibility",
            "status": "active" if lighthouse.get("ok") else "setup needed",
            "detail": f"{lighthouse.get('issue_count', 0)} issues, {lighthouse.get('resolved_count', 0)} resolved",
            "metric": lighthouse.get("scores", {}).get("Accessibility", "n/a"),
            "action": "Review issues",
            "href": "#issues",
        },
        {
            "name": "SEO",
            "status": "active" if lighthouse.get("ok") else "setup needed",
            "detail": f"{lighthouse.get('scores', {}).get('SEO', 'n/a')} Lighthouse SEO score",
            "metric": lighthouse.get("scores", {}).get("SEO", "n/a"),
            "action": "Open SEO",
            "href": "#seo",
        },
        {
            "name": "Performance",
            "status": "active" if lighthouse.get("ok") else "setup needed",
            "detail": "Core Web Vitals from the latest full Lighthouse run",
            "metric": lighthouse.get("scores", {}).get("Performance", "n/a"),
            "action": "Open report",
            "href": lighthouse.get("report_href", "#performance"),
        },
        {
            "name": "Standards Engine",
            "status": "active" if lighthouse.get("ok") else "setup needed",
            "detail": "WCAG, ACT-style rule mapping, and export-ready conformance metadata",
            "metric": "ACT",
            "action": "Open standards",
            "href": "#standards",
        },
        {
            "name": "Link Integrity",
            "status": "on demand",
            "detail": "Run link checks when you need a broken-link audit",
            "metric": "Manual",
            "action": "View reports",
            "href": "#reports",
        },
        {
            "name": "Report Center",
            "status": "active" if reports else "empty",
            "detail": f"{len(reports)} generated artifacts for {len(targets)} tracked URL(s)",
            "metric": str(len(reports)),
            "action": "Open reports",
            "href": "#reports",
        },
    ]

    return modules


def load_technical_service_statuses() -> list[dict[str, str]]:
    checks = [
        {
            "name": "OpenAudit Hub",
            "url": "local",
            "status": "online",
            "detail": "Serving this dashboard",
        },
        {
            "name": "Pa11y Dashboard",
            "url": os.getenv("PA11Y_URL", "http://localhost:4000"),
            "status": "unknown",
            "detail": "Open in browser",
        },
        {
            "name": "Lighthouse CI",
            "url": os.getenv("LHCI_URL", "http://localhost:9001"),
            "status": "unknown",
            "detail": "Open in browser",
        },
        {
            "name": "Oobee",
            "url": "tool",
            "status": "on demand",
            "detail": "Run deep scans from the script",
        },
        {
            "name": "LinkChecker",
            "url": "tool",
            "status": "on demand",
            "detail": "Run link checks from the script",
        },
    ]

    lhci = LhciClient()
    if lhci.ping():
        checks[2]["status"] = "online"
        checks[2]["detail"] = "API reachable"
    else:
        checks[2]["status"] = "offline"
        checks[2]["detail"] = lhci.error or "API not reachable"

    return checks


class LhciClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("LHCI_API_URL", "http://lhci-server:9001").rstrip("/")
        self.username = os.getenv("LHCI_BASIC_AUTH_USERNAME", "admin")
        self.password = os.getenv("LHCI_BASIC_AUTH_PASSWORD", "change-me")
        self.ok = True
        self.error = ""

    def get_json(self, path: str) -> Any:
        try:
            response = requests.get(
                f"{self.base_url}{path}",
                auth=(self.username, self.password),
                timeout=2.5,
            )
            response.raise_for_status()
            self.ok = True
            return response.json()
        except requests.RequestException as exc:
            self.ok = False
            self.error = str(exc)
            return None

    def ping(self) -> bool:
        try:
            response = requests.get(
                f"{self.base_url}/version",
                auth=(self.username, self.password),
                timeout=2.5,
            )
            response.raise_for_status()
            self.ok = True
            return True
        except requests.RequestException as exc:
            self.ok = False
            self.error = str(exc)
            return False


def format_build(build: dict[str, Any] | None) -> dict[str, str] | None:
    if not build:
        return None

    created_at = build.get("createdAt") or build.get("runAt") or ""
    return {
        "id": str(build.get("id", "")),
        "hash": short_hash(str(build.get("hash", ""))),
        "branch": str(build.get("branch", "main")),
        "message": str(build.get("message", "Manual Lighthouse run")),
        "created_at": format_date(created_at),
    }


def extract_scores(stats: list[dict[str, Any]]) -> dict[str, str]:
    labels = {
        "category_performance_median": "Performance",
        "category_accessibility_median": "Accessibility",
        "category_best-practices_median": "Best Practices",
        "category_seo_median": "SEO",
    }
    scores = {label: "n/a" for label in labels.values()}

    for item in stats:
        name = str(item.get("name", "")).lower()
        value = item.get("value")
        if name in labels and isinstance(value, (int, float)):
            scores[labels[name]] = f"{round(value * 100)}"

    return scores


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").split())


def meta_content(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name})
    if not tag:
        return ""
    return normalize_text(tag.get("content", ""))


def meta_property(soup: BeautifulSoup, property_name: str) -> str:
    tag = soup.find("meta", attrs={"property": property_name})
    if not tag:
        return ""
    return normalize_text(tag.get("content", ""))


def link_href(soup: BeautifulSoup, rel: str) -> str:
    tag = soup.find("link", rel=lambda values: values and rel in values)
    if not tag:
        return ""
    return str(tag.get("href", "")).strip()


def short_hash(value: str) -> str:
    if len(value) <= 10:
        return value or "manual"
    return value[:10]


def format_date(value: str) -> str:
    if not value:
        return "n/a"
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def format_lighthouse_fetch_time(value: str, fallback_path: Path) -> str:
    if value:
        formatted = format_date(value)
        if formatted != value:
            return formatted
    return datetime.fromtimestamp(
        fallback_path.stat().st_mtime, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


def format_short_date(value: str, fallback_path: Path) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.fromtimestamp(fallback_path.stat().st_mtime, tz=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%m/%d %H:%M")


def estimate_next_crawl_time(value: str) -> str:
    interval = int(os.getenv("LHCI_SCHEDULE_INTERVAL_MINUTES", "1440"))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed + timedelta(minutes=interval)).astimezone(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except ValueError:
        return f"{interval} minutes after the next scheduled run"


def score_label(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    if value >= 0.9:
        return "good"
    if value >= 0.5:
        return "needs-work"
    return "poor"


def detect_kind(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("sitemap-") and lowered.endswith(".json"):
        return "Sitemap crawl report"
    if "oobee" in lowered:
        return "Deep accessibility audit"
    if "pa11y" in lowered:
        return "Pa11y accessibility report"
    if "lighthouse" in lowered:
        return "Google Lighthouse report"
    if "linkcheck" in lowered:
        return "Broken link report"
    if lowered.endswith(".html"):
        return "HTML report"
    if lowered.endswith(".zip"):
        return "Archive"
    if lowered.endswith(".txt"):
        return "Text report"
    return "Report artifact"


def format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

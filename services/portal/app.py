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
            issue=issue,
            lighthouse=summary,
            pa11y_url=os.getenv("PA11Y_URL", "http://localhost:4000"),
            lhci_url=os.getenv("LHCI_URL", "http://localhost:9001"),
            selected_site_key=site_key(site) if site else "",
            selected_site_url=site or summary.get("final_url", ""),
            selected_site_label=site_label(site) if site else site_label(str(summary.get("final_url", ""))),
            site_options=load_site_options(),
        )

    @app.route("/api/status")
    def status():
        return jsonify(load_service_statuses())

    @app.route("/api/seo")
    def seo():
        return jsonify(load_seo_summary())

    @app.route("/reports/<path:filename>")
    def report_file(filename: str):
        return send_from_directory(REPORTS_DIR, filename, as_attachment=False)

    @app.after_request
    def remember_selected_site(response):
        requested = request.args.get("site", "").strip()
        if requested and any(requested == site.get("key") for site in load_site_options()):
            response.set_cookie("openaudit_site", requested, max_age=60 * 60 * 24 * 90, samesite="Lax")
        return response

    return app


def portal_template_context(active_slug: str = "dashboard") -> dict[str, Any]:
    sites = load_site_options()
    selected_site = select_site(sites)
    selected_site_url = selected_site.get("url") or os.getenv(
        "DEFAULT_TARGET_URL", "https://example.gov.au"
    )
    selected_site_key = selected_site.get("key", "")
    managed_issues = database_issues(selected_site_key) if selected_site_key else []
    return {
        "title": os.getenv("PORTAL_TITLE", "OpenAudit Hub"),
        "subtitle": os.getenv(
            "PORTAL_SUBTITLE",
            "Open-source website quality operations center",
        ),
        "pa11y_url": os.getenv("PA11Y_URL", "http://localhost:4000"),
        "lhci_url": os.getenv("LHCI_URL", "http://localhost:9001"),
        "default_target_url": selected_site_url,
        "selected_site": selected_site,
        "selected_site_key": selected_site_key,
        "site_options": sites,
        "targets": [site["url"] for site in sites if site.get("url")],
        "reports": load_reports(selected_site_url),
        "crawl_pages": list_crawl_pages(selected_site_key) if selected_site_key else [],
        "managed_issues": managed_issues,
        "pa11y_issues": [
            {**issue, "guidance": build_pa11y_guidance(issue)}
            for issue in managed_issues
            if issue.get("source") == "pa11y"
        ],
        "lighthouse": load_lighthouse_report_summary(selected_site_url),
        "module_nav": module_navigation(active_slug),
        "active_slug": active_slug,
    }


def module_navigation(active_slug: str = "") -> list[dict[str, Any]]:
    groups = [
        {
            "group": "Quality Assurance",
            "slug": "quality-assurance",
            "items": [
                {"title": "Issues and recommendations", "slug": "issues"},
                {"title": "Issue detail preview", "slug": "issue-detail-preview"},
                {"title": "Pages with issues", "slug": "pages"},
                {"title": "Site architecture", "slug": "site-architecture"},
                {"title": "Crawl comparison", "slug": "crawl-comparison"},
                {"title": "Broken links", "slug": "broken-links"},
                {"title": "Resolved issues", "slug": "resolved"},
                {"title": "Prepublish checks", "slug": "prepublish"},
            ],
        },
        {
            "group": "Accessibility",
            "slug": "accessibility",
            "items": [
                {"title": "Accessibility overview", "slug": "accessibility-overview"},
                {"title": "Accessibility issues", "slug": "accessibility-issues"},
                {"title": "Guidelines and standards", "slug": "standards"},
            ],
        },
        {
            "group": "SEO Advanced",
            "slug": "seo",
            "badge": "New",
            "items": [
                {"title": "SEO overview", "slug": "seo-advanced"},
                {"title": "Content optimization", "slug": "content-optimization"},
                {"title": "Duplicate content", "slug": "duplicate-content"},
                {"title": "Keyword suggestions", "slug": "keyword-suggestions"},
                {"title": "Robots and indexing", "slug": "robots-indexing"},
                {"title": "Structured data", "slug": "structured-data"},
                {"title": "Sitemaps", "slug": "sitemaps"},
            ],
        },
        {
            "group": "Content",
            "slug": "content",
            "items": [
                {"title": "Content quality", "slug": "content-quality"},
                {"title": "Spelling and readability", "slug": "spelling-readability"},
                {"title": "Documents and PDFs", "slug": "documents-pdfs"},
            ],
        },
        {
            "group": "Marketing Analytics",
            "slug": "analytics",
            "items": [
                {"title": "Analytics overview", "slug": "analytics-overview"},
                {"title": "Campaigns", "slug": "campaigns"},
                {"title": "Behaviour maps", "slug": "behaviour-maps"},
                {"title": "Email marketing", "slug": "email-marketing"},
            ],
        },
        {
            "group": "Publishing",
            "slug": "publishing",
            "items": [
                {"title": "Drupal / SDP publishing", "slug": "drupal-sdp-publishing"},
            ],
        },
        {
            "group": "Administration",
            "slug": "administration",
            "items": [
                {"title": "Policy", "slug": "policy"},
                {"title": "Privacy and cookies", "slug": "privacy-cookies"},
                {"title": "Uptime and response", "slug": "uptime-response"},
                {"title": "Integrations", "slug": "integrations"},
                {"title": "AI recommendations", "slug": "ai-recommendations"},
                {"title": "Reports", "slug": "reports"},
                {"title": "Feature coverage", "slug": "feature-coverage"},
            ],
        },
    ]
    for group in groups:
        group["open"] = any(item["slug"] == active_slug for item in group["items"])
        for item in group["items"]:
            item["active"] = item["slug"] == active_slug
    return groups


def module_pages() -> dict[str, dict[str, str]]:
    pages: dict[str, dict[str, str]] = {}
    for group in module_navigation():
        for item in group["items"]:
            pages[item["slug"]] = {
                "slug": item["slug"],
                "title": item["title"],
                "group": group["group"],
            }
    return pages


def find_module_page(slug: str) -> dict[str, str] | None:
    return module_pages().get(slug)


def is_analytics_page(slug: str) -> bool:
    return slug in {"analytics-overview", "campaigns", "behaviour-maps"}


def is_connector_page(slug: str) -> bool:
    return slug in {"email-marketing", "drupal-sdp-publishing"}


def load_targets() -> list[str]:
    return [str(website.get("base_url") or "") for website in list_websites(active_only=True) if website.get("base_url")]


def load_site_options() -> list[dict[str, str]]:
    seen = {
        str(website["key"]): {
            "key": str(website["key"]),
            "url": str(website["base_url"]),
            "label": str(website["name"]),
            "source": "database",
        }
        for website in list_websites(active_only=True)
    }

    if not seen:
        fallback = os.getenv("DEFAULT_TARGET_URL", "https://example.gov.au")
        seen[site_key(fallback)] = {
            "key": site_key(fallback),
            "url": fallback,
            "label": site_label(fallback),
            "source": "default",
        }
    return sorted(seen.values(), key=lambda item: item["label"].lower())


def select_site(sites: list[dict[str, str]]) -> dict[str, str]:
    requested = request.args.get("site", "").strip() if request else ""
    remembered = request.cookies.get("openaudit_site", "").strip() if request else ""
    requested = requested or remembered
    if requested:
        for site in sites:
            if requested in {site.get("key", ""), site.get("url", ""), site.get("label", "")}:
                return site
    return sites[0] if sites else {}


def resolve_requested_site_url() -> str | None:
    site = select_site(load_site_options())
    return site.get("url") or None


def site_label(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or url.replace("https://", "").replace("http://", "").strip("/")


def site_key(url: str) -> str:
    label = site_label(url).lower()
    return "".join(ch if ch.isalnum() else "-" for ch in label).strip("-") or "site"


def site_query(site: dict[str, str]) -> str:
    return quote(site.get("key", ""), safe="")


def issue_href(issue_id: str, site_url: str | None = None) -> str:
    if not site_url:
        return f"/issues/{quote(issue_id, safe='')}"
    return f"/issues/{quote(issue_id, safe='')}?site={quote(site_key(site_url), safe='')}"


def load_reports(site_url: str | None = None) -> list[dict[str, str]]:
    if not REPORTS_DIR.exists():
        return []

    reports = []
    for path in sorted(
        REPORTS_DIR.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True
    ):
        if path.name.startswith("."):
            continue
        if site_url and is_site_scoped_report(path) and not report_matches_site(path, site_url):
            continue
        stat = path.stat()
        reports.append(
            {
                "name": path.name,
                "href": f"/reports/{path.name}",
                "kind": detect_kind(path.name),
                "size": format_size(stat.st_size),
                "updated": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC"),
            }
        )
    return reports[:20]


def load_lighthouse_report_summary(site_url: str | None = None) -> dict[str, Any]:
    json_report = latest_report_file(".report.json", site_url)
    if not json_report:
        return {
            "ok": False,
            "error": "No Google Lighthouse JSON report has been generated yet.",
            "overview_score": "n/a",
            "score_history": [],
            "audit_history": build_audit_history(site_url),
            "scores": {},
            "score_breakdown": [],
            "metrics": [],
            "opportunities": [],
            "issues_to_fix": [],
            "resolved_checks": [],
            "role_buckets": [],
            "page_overview": {},
            "pages_with_issues": [],
            "policy_summary": [],
            "inventory": [],
            "score_impact": [],
            "action_queue": [],
            "crawl_events": [],
            "standards_engine": build_standards_engine_summary([]),
            "capability_matrix": build_capability_matrix([], {}, []),
            "integration_summary": build_integration_summary(),
            "export_formats": build_export_formats(),
            "feature_modules": build_feature_modules([], {}, []),
            "seo_advanced": build_seo_advanced_summary({}, {}),
            "keyword_suggestions": build_keyword_suggestions({}, {}, [], []),
            "accessibility_breakdown": build_accessibility_breakdown({}, []),
            "prepublish_summary": build_prepublish_summary([]),
            "analytics_summary": build_analytics_summary({}, [], []),
            "campaign_summary": build_campaign_summary(),
            "behavior_summary": build_behavior_summary({}),
            "content_quality": build_content_quality_summary({}),
            "link_integrity": build_link_integrity_summary([]),
            "document_governance": build_document_governance_summary([]),
            "privacy_summary": build_privacy_summary({}),
            "response_summary": build_response_summary({}),
            "connector_summary": build_connector_summary([]),
            "crawler_summary": build_crawler_summary({}, [], []),
            "architecture_summary": build_architecture_summary({}, []),
            "comparison_summary": build_comparison_summary([]),
            "ai_recommendations": build_ai_recommendations([]),
            "report_href": "",
            "json_href": "",
            "final_url": "",
            "generated_at": "",
            "report_source": {},
        }

    cache_key = f"{lighthouse_cache_key(json_report)}:{report_collection_cache_key(site_url)}:{search_console_cache_key(site_url)}"
    if _LIGHTHOUSE_SUMMARY_CACHE.get("key") == cache_key:
        return _LIGHTHOUSE_SUMMARY_CACHE["value"]

    try:
        data = json.loads(json_report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "error": f"Unable to read Lighthouse report: {exc}",
            "overview_score": "n/a",
            "score_history": [],
            "audit_history": build_audit_history(site_url),
            "scores": {},
            "score_breakdown": [],
            "metrics": [],
            "opportunities": [],
            "issues_to_fix": [],
            "resolved_checks": [],
            "role_buckets": [],
            "page_overview": {},
            "pages_with_issues": [],
            "policy_summary": [],
            "inventory": [],
            "score_impact": [],
            "action_queue": [],
            "crawl_events": [],
            "standards_engine": build_standards_engine_summary([]),
            "capability_matrix": build_capability_matrix([], {}, []),
            "integration_summary": build_integration_summary(),
            "export_formats": build_export_formats(),
            "feature_modules": build_feature_modules([], {}, []),
            "seo_advanced": build_seo_advanced_summary({}, {}),
            "keyword_suggestions": build_keyword_suggestions({}, {}, [], []),
            "accessibility_breakdown": build_accessibility_breakdown({}, []),
            "prepublish_summary": build_prepublish_summary([]),
            "analytics_summary": build_analytics_summary({}, [], []),
            "campaign_summary": build_campaign_summary(),
            "behavior_summary": build_behavior_summary({}),
            "content_quality": build_content_quality_summary({}),
            "link_integrity": build_link_integrity_summary([]),
            "document_governance": build_document_governance_summary([]),
            "privacy_summary": build_privacy_summary({}),
            "response_summary": build_response_summary({}),
            "connector_summary": build_connector_summary([]),
            "crawler_summary": build_crawler_summary({}, [], []),
            "architecture_summary": build_architecture_summary({}, []),
            "comparison_summary": build_comparison_summary([]),
            "ai_recommendations": build_ai_recommendations([]),
            "report_href": "",
            "json_href": f"/reports/{json_report.name}",
            "final_url": "",
            "generated_at": "",
            "report_source": {},
        }

    html_report = json_report.with_name(json_report.name.replace(".report.json", ".report.html"))
    audits = data.get("audits", {})
    categories = data.get("categories", {})
    issues_to_fix = extract_lighthouse_issues(categories, audits)
    effective_site_url = (
        site_url
        or data.get("finalDisplayedUrl")
        or data.get("finalUrl")
        or data.get("requestedUrl")
        or ""
    )
    attach_issue_hrefs(issues_to_fix, str(effective_site_url) if effective_site_url else None)
    for issue in issues_to_fix:
        issue["page_url"] = str(effective_site_url)
    lifecycle_summary = {"opened": 0, "reopened": 0, "resolved": 0, "current": len(issues_to_fix)}
    if effective_site_url and get_website(site_key(str(effective_site_url))):
        website_key_value = site_key(str(effective_site_url))
        lifecycle_rows = database_issues(website_key_value)
        if not lifecycle_rows:
            lifecycle_summary = reconcile_issues(website_key_value, issues_to_fix, json_report.name)
            lifecycle_rows = database_issues(website_key_value)
        else:
            lifecycle_summary = {
                "opened": sum(1 for row in lifecycle_rows if row.get("status") == "open"),
                "reopened": sum(1 for row in lifecycle_rows if row.get("status") == "reopened"),
                "resolved": sum(1 for row in lifecycle_rows if row.get("status") == "resolved"),
                "current": sum(1 for row in lifecycle_rows if row.get("status") not in {"resolved", "ignored"}),
            }
        lifecycle_by_audit = {str(row.get("audit_id") or row.get("issue_key")): row for row in lifecycle_rows}
        for issue in issues_to_fix:
            lifecycle = lifecycle_by_audit.get(str(issue.get("id") or ""), {})
            issue["lifecycle_id"] = lifecycle.get("id", "")
            issue["lifecycle_status"] = lifecycle.get("status", "open")
            issue["lifecycle_owner"] = lifecycle.get("owner", issue.get("responsibility", "Unassigned"))
            issue["lifecycle_evidence"] = lifecycle.get("evidence", [])
            if not issue.get("affected_examples") and lifecycle.get("evidence"):
                issue["affected_examples"] = lifecycle["evidence"]
    resolved_checks = extract_resolved_checks(categories, audits)
    remediation_issue = select_remediation_issue(issues_to_fix)
    reports = load_reports(site_url)

    summary = {
        "ok": True,
        "error": "",
        "overview_score": calculate_overview_score(categories),
        "score_history": build_score_history(site_url),
        "audit_history": build_audit_history(site_url),
        "scores": extract_lighthouse_scores(categories),
        "score_breakdown": extract_score_breakdown(categories),
        "metrics": extract_lighthouse_metrics(audits),
        "opportunities": extract_lighthouse_opportunities(audits),
        "issues_to_fix": issues_to_fix,
        "lifecycle_summary": lifecycle_summary,
        "remediation_issue": remediation_issue,
        "resolved_checks": resolved_checks,
        "role_buckets": group_issues_by_role(issues_to_fix),
        "page_overview": extract_page_overview(data, audits),
        "pages_with_issues": extract_pages_with_issues(data, issues_to_fix),
        "policy_summary": build_policy_summary(issues_to_fix),
        "inventory": build_inventory_summary(data, audits),
        "score_impact": build_score_impact(issues_to_fix),
        "action_queue": build_action_queue(issues_to_fix),
        "crawl_events": build_crawl_events(json_report, site_url),
        "score_gain_available": score_gain_available(issues_to_fix),
        "top_priority": issues_to_fix[0] if issues_to_fix else None,
        "standards_engine": build_standards_engine_summary(issues_to_fix),
        "capability_matrix": build_capability_matrix(issues_to_fix, audits, reports),
        "integration_summary": build_integration_summary(),
        "export_formats": build_export_formats(),
        "feature_modules": build_feature_modules(issues_to_fix, audits, reports),
        "seo_advanced": build_seo_advanced_summary(categories, audits),
        "keyword_suggestions": build_keyword_suggestions(data, audits, issues_to_fix, reports),
        "accessibility_breakdown": build_accessibility_breakdown(categories, issues_to_fix),
        "prepublish_summary": build_prepublish_summary(issues_to_fix),
        "analytics_summary": build_analytics_summary(audits, issues_to_fix, reports),
        "campaign_summary": build_campaign_summary(),
        "behavior_summary": build_behavior_summary(audits),
        "content_quality": build_content_quality_summary(audits),
        "link_integrity": build_link_integrity_summary(reports),
        "document_governance": build_document_governance_summary(reports),
        "privacy_summary": build_privacy_summary(audits),
        "response_summary": build_response_summary(audits),
        "connector_summary": build_connector_summary(reports),
        "crawler_summary": build_crawler_summary(data, audits, reports),
        "architecture_summary": build_architecture_summary(data, issues_to_fix),
        "comparison_summary": build_comparison_summary(reports),
        "ai_recommendations": build_ai_recommendations(issues_to_fix),
        "issue_count": len(issues_to_fix),
        "resolved_count": len(resolved_checks),
        "potential_count": count_potential_checks(audits),
        "report_href": f"/reports/{html_report.name}" if html_report.exists() else "",
        "json_href": f"/reports/{json_report.name}",
        "final_url": data.get("finalDisplayedUrl") or data.get("finalUrl") or "",
        "generated_at": format_lighthouse_fetch_time(
            str(data.get("fetchTime", "")), json_report
        ),
        "next_crawl_at": estimate_next_crawl_time(str(data.get("fetchTime", ""))),
    }
    summary["report_source"] = {
        "site": site_label(str(effective_site_url)) if effective_site_url else "",
        "url": summary["final_url"],
        "json": json_report.name,
        "html": html_report.name if html_report.exists() else "",
        "generated_at": summary["generated_at"],
    }
    _LIGHTHOUSE_SUMMARY_CACHE["key"] = cache_key
    _LIGHTHOUSE_SUMMARY_CACHE["value"] = summary
    return summary


def lighthouse_cache_key(json_report: Path) -> str:
    try:
        stat = json_report.stat()
    except OSError:
        return str(json_report)
    return f"{json_report}:{stat.st_mtime_ns}:{stat.st_size}"


def report_collection_cache_key(site_url: str | None = None) -> str:
    if not REPORTS_DIR.exists():
        return "no-reports"
    parts = []
    for path in REPORTS_DIR.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        if site_url and is_site_scoped_report(path) and not report_matches_site(path, site_url):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(sorted(parts)) or "empty"


def search_console_cache_key(site_url: str | None = None) -> str:
    parts = []
    for directory in SEARCH_CONSOLE_DIRS:
        if not directory.exists():
            continue
        for path in directory.glob("*.csv"):
            try:
                stat = path.stat()
            except OSError:
                continue
            parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(sorted(parts)) or "no-search-console"


def latest_report_file(suffix: str, site_url: str | None = None) -> Path | None:
    if not REPORTS_DIR.exists():
        return None

    matches = [
        path
        for path in REPORTS_DIR.iterdir()
        if path.is_file()
        and path.name.lower().startswith("lighthouse-")
        and path.name.lower().endswith(suffix)
    ]
    if site_url:
        matches = [path for path in matches if report_matches_site(path, site_url)]
    if not matches:
        return None
    return max(matches, key=lambda item: item.stat().st_mtime)


def report_matches_site(path: Path, site_url: str) -> bool:
    expected = site_key(site_url)
    candidate = path
    if path.name.endswith(".report.html"):
        candidate = path.with_name(path.name.replace(".report.html", ".report.json"))
    if candidate.exists() and candidate.name.endswith(".report.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            urls = [
                str(data.get("finalDisplayedUrl") or ""),
                str(data.get("finalUrl") or ""),
                str(data.get("requestedUrl") or ""),
            ]
            return any(site_key(url) == expected for url in urls if url)
        except (OSError, json.JSONDecodeError):
            pass
    return expected in path.name.lower()


def is_site_scoped_report(path: Path) -> bool:
    return path.name.lower().startswith(("lighthouse-", "pa11y-", "sitemap-", "linkcheck-", "oobee-"))


def lighthouse_json_files(site_url: str | None = None) -> list[Path]:
    if not REPORTS_DIR.exists():
        return []
    files = sorted(
        [
            path
            for path in REPORTS_DIR.iterdir()
            if path.is_file()
            and path.name.lower().startswith("lighthouse-")
            and path.name.lower().endswith(".report.json")
        ],
        key=lambda item: item.stat().st_mtime,
    )
    if site_url:
        files = [path for path in files if report_matches_site(path, site_url)]
    return files


def build_score_history(site_url: str | None = None) -> list[dict[str, str]]:
    history = []
    for path in lighthouse_json_files(site_url)[-12:]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        score = calculate_overview_score(data.get("categories", {}))
        fetch_time = str(data.get("fetchTime", ""))
        label = format_short_date(fetch_time, path)
        bar = score if score != "n/a" else "0"
        history.append({"label": label, "score": score, "bar": bar})
    return history


def build_audit_history(site_url: str | None = None) -> dict[str, Any]:
    snapshots = []
    for path in lighthouse_json_files(site_url)[-12:]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        categories = data.get("categories", {})
        audits = data.get("audits", {})
        fetch_time = str(data.get("fetchTime", ""))
        scores = category_score_map(categories)
        overview = numeric_overview_score(categories)
        issues = issue_change_index(extract_lighthouse_issues(categories, audits))
        snapshots.append(
            {
                "report": path.name,
                "label": format_short_date(fetch_time, path),
                "updated": format_lighthouse_fetch_time(fetch_time, path),
                "url": data.get("finalDisplayedUrl") or data.get("finalUrl") or data.get("requestedUrl") or "",
                "overview": overview,
                "scores": scores,
                "_issues": issues,
            }
        )

    rows = []
    previous = None
    for snapshot in snapshots:
        delta = None
        if previous and snapshot["overview"] is not None and previous["overview"] is not None:
            delta = snapshot["overview"] - previous["overview"]
        category_deltas = {}
        if previous:
            for category, value in snapshot["scores"].items():
                old_value = previous["scores"].get(category)
                if value is not None and old_value is not None:
                    category_deltas[category] = value - old_value
        rows.append(
            {
                **snapshot,
                "delta": delta,
                "delta_label": format_delta(delta),
                "category_deltas": category_deltas,
                "issue_count": len(snapshot.get("_issues", {})),
                "regressions": [
                    {"category": category, "delta": format_delta(value)}
                    for category, value in category_deltas.items()
                    if value < 0
                ],
            }
        )
        previous = snapshot

    latest = rows[-1] if rows else None
    previous_row = rows[-2] if len(rows) > 1 else None
    issue_changes = build_issue_changes(latest, previous_row)
    public_rows = [
        {key: value for key, value in row.items() if key != "_issues"}
        for row in rows
    ]
    delta = latest.get("delta") if latest else None
    category_trends = build_category_trends(public_rows)
    regression_count = sum(
        1 for value in (latest or {}).get("category_deltas", {}).values() if value < 0
    )
    alerts = build_regression_alerts(latest, public_rows)
    risk_alerts = [
        alert for alert in alerts if alert.get("severity") in {"High", "Medium"}
    ]
    return {
        "status": "Needs attention" if risk_alerts else "Ready" if len(rows) > 1 else "Needs more crawls",
        "health": "At risk" if risk_alerts else "Healthy" if len(rows) > 1 else "Collecting baseline",
        "latest_score": str(latest.get("overview")) if latest and latest.get("overview") is not None else "n/a",
        "previous_score": str(previous_row.get("overview")) if previous_row and previous_row.get("overview") is not None else "n/a",
        "delta": format_delta(delta),
        "direction": trend_direction(delta),
        "crawl_count": str(len(rows)),
        "regression_count": str(regression_count),
        "alerts": alerts,
        "issue_changes": issue_changes,
        "rows": list(reversed(public_rows)),
        "category_trends": category_trends,
        "next_step": "Schedule repeated Lighthouse runs to make regressions and improvements visible over time.",
    }


def issue_change_index(issues: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for issue in issues:
        key = issue_identity(issue)
        if not key:
            continue
        indexed[key] = {
            "id": str(issue.get("id") or ""),
            "title": str(issue.get("title") or "Untitled issue"),
            "category": str(issue.get("category") or "General"),
            "points": str(issue.get("points") or "0"),
            "difficulty": str(issue.get("difficulty") or "Review"),
            "occurrences": str(issue.get("occurrences") or "0"),
        }
    return indexed


def issue_identity(issue: dict[str, Any]) -> str:
    raw = issue.get("id") or f"{issue.get('category', '')}:{issue.get('title', '')}"
    return re.sub(r"[^a-z0-9]+", "-", str(raw).lower()).strip("-")


def build_issue_changes(
    latest: dict[str, Any] | None, previous: dict[str, Any] | None
) -> dict[str, Any]:
    if not latest:
        return {
            "status": "No crawls",
            "new_count": "0",
            "resolved_count": "0",
            "new_issues": [],
            "resolved_issues": [],
            "summary": "Run Lighthouse to start tracking issue movement.",
        }
    if not previous:
        return {
            "status": "Baseline",
            "new_count": str(len(latest.get("_issues", {}))),
            "resolved_count": "0",
            "new_issues": list(latest.get("_issues", {}).values())[:5],
            "resolved_issues": [],
            "summary": "This is the baseline crawl. Run the same site again to see new and resolved issues.",
        }

    latest_issues = latest.get("_issues", {})
    previous_issues = previous.get("_issues", {})
    new_keys = sorted(set(latest_issues) - set(previous_issues))
    resolved_keys = sorted(set(previous_issues) - set(latest_issues))
    improved = len(resolved_keys) > len(new_keys)
    if new_keys and not resolved_keys:
        summary = "New issues appeared in the latest crawl. Review these first before chasing smaller score changes."
    elif resolved_keys and not new_keys:
        summary = "No new issues appeared, and previous checks were resolved. This is a clean improvement."
    elif new_keys or resolved_keys:
        summary = "The latest crawl has a mixed result: some issues were fixed while others appeared."
    else:
        summary = "The issue list is stable. Use category deltas to inspect score changes."

    return {
        "status": "Improved" if improved else "Changed" if new_keys or resolved_keys else "Stable",
        "new_count": str(len(new_keys)),
        "resolved_count": str(len(resolved_keys)),
        "new_issues": [latest_issues[key] for key in new_keys[:5]],
        "resolved_issues": [previous_issues[key] for key in resolved_keys[:5]],
        "summary": summary,
    }


def category_score_map(categories: dict[str, Any]) -> dict[str, int | None]:
    mapping = {
        "Performance": "performance",
        "Accessibility": "accessibility",
        "Best Practices": "best-practices",
        "SEO": "seo",
    }
    return {
        label: category_score_value(categories.get(key, {}))
        for label, key in mapping.items()
    }


def category_score_value(category: Any) -> int | None:
    if not isinstance(category, dict):
        return None
    score = category.get("score")
    if not isinstance(score, (int, float)):
        return None
    return round(score * 100)


def numeric_overview_score(categories: dict[str, Any]) -> int | None:
    score = calculate_overview_score(categories)
    if score == "n/a":
        return None
    return int(score)


def format_delta(delta: int | None) -> str:
    if delta is None:
        return "-"
    return f"{delta:+d}"


def trend_direction(delta: int | None) -> str:
    if delta is None:
        return "No baseline"
    if delta > 0:
        return "Improving"
    if delta < 0:
        return "Regressing"
    return "Stable"


def build_category_trends(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not rows:
        return []
    latest = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    trends = []
    for category, score in latest.get("scores", {}).items():
        old_score = previous.get("scores", {}).get(category) if previous else None
        delta = score - old_score if score is not None and old_score is not None else None
        trends.append(
            {
                "category": category,
                "score": str(score) if score is not None else "n/a",
                "delta": format_delta(delta),
                "direction": trend_direction(delta),
                "bar": str(score or 0),
            }
        )
    return trends


def build_regression_alerts(
    latest: dict[str, Any] | None, rows: list[dict[str, Any]]
) -> list[dict[str, str]]:
    if not latest:
        return []
    if len(rows) < 2:
        return [
            {
                "severity": "Info",
                "title": "Baseline needed",
                "detail": "Run another Lighthouse audit for this same site to unlock regression detection.",
            }
        ]
    alerts = []
    overview_delta = latest.get("delta")
    if isinstance(overview_delta, int) and overview_delta <= -5:
        alerts.append(
            {
                "severity": "High",
                "title": "Overall score dropped",
                "detail": f"Digital Certainty score changed by {format_delta(overview_delta)} since the previous crawl.",
            }
        )
    for category, delta in latest.get("category_deltas", {}).items():
        if delta <= -10:
            severity = "High"
        elif delta <= -5:
            severity = "Medium"
        else:
            continue
        alerts.append(
            {
                "severity": severity,
                "title": f"{category} regression",
                "detail": f"{category} changed by {format_delta(delta)} since the previous crawl.",
            }
        )
    if not alerts and isinstance(overview_delta, int) and overview_delta > 0:
        alerts.append(
            {
                "severity": "Positive",
                "title": "Score improved",
                "detail": f"Overall score improved by {format_delta(overview_delta)} since the previous crawl.",
            }
        )
    return alerts[:5]


def calculate_overview_score(categories: dict[str, Any]) -> str:
    keys = ["performance", "accessibility", "best-practices", "seo"]
    values = [
        categories.get(key, {}).get("score")
        for key in keys
        if isinstance(categories.get(key, {}).get("score"), (int, float))
    ]
    if not values:
        return "n/a"
    return f"{round((sum(values) / len(values)) * 100)}"


def score_gain_available(issues: list[dict[str, Any]]) -> str:
    points = sum(float(issue.get("points", "0")) for issue in issues[:8])
    return f"{points:.1f}"


def attach_issue_hrefs(issues: list[dict[str, Any]], site_url: str | None = None) -> None:
    for issue in issues:
        issue["href"] = issue_href(str(issue.get("id", "")), site_url)


def build_score_impact(issues: list[dict[str, Any]]) -> list[dict[str, str]]:
    categories = ["Accessibility", "Performance", "SEO", "Best Practices"]
    rows = []
    for category in categories:
        matches = [issue for issue in issues if issue.get("category") == category]
        points = sum(float(issue.get("points", "0")) for issue in matches)
        rows.append(
            {
                "category": category,
                "issues": str(len(matches)),
                "points": f"{points:.1f}",
                "bar": f"{min(100, max(0, points)):.0f}",
            }
        )
    return rows


def build_action_queue(issues: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for index, issue in enumerate(issues[:8], start=1):
        points = safe_float(issue.get("points"), 0)
        owner = str(issue.get("responsibility") or issue.get("role") or "Owner")
        category = str(issue.get("category") or "General")
        title = str(issue.get("title") or "Untitled issue")
        rows.append(
            {
                "rank": str(index),
                "title": title,
                "owner": owner,
                "category": category,
                "points": f"{points:.1f}",
                "priority": action_priority(points),
                "status": action_status(issue),
                "due": action_due_label(points),
                "scope": action_scope(issue),
                "next_step": action_next_step(issue),
                "validation": action_validation_label(issue),
                "href": issue.get("href", f"/issues/{issue.get('id', '')}"),
            }
        )
    return rows


def safe_float(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def action_priority(points: float) -> str:
    if points >= 10:
        return "Critical"
    if points >= 3:
        return "High"
    if points >= 1:
        return "Medium"
    return "Low"


def action_due_label(points: float) -> str:
    if points >= 10:
        return "This week"
    if points >= 3:
        return "Next sprint"
    return "Backlog"


def action_status(issue: dict[str, Any]) -> str:
    if issue.get("affected_examples"):
        return "Ready to assign"
    if issue.get("category") == "Performance":
        return "Needs report review"
    return "Needs evidence"


def action_scope(issue: dict[str, Any]) -> str:
    occurrences = str(issue.get("occurrences") or "0")
    pages = str(issue.get("pages") or "1")
    element = str(issue.get("element") or "Element")
    return f"{occurrences} occurrence(s), {pages} page(s), {element}"


def action_next_step(issue: dict[str, Any]) -> str:
    guidance = issue.get("fix_guidance") or {}
    steps = guidance.get("steps") or []
    if steps:
        return str(steps[0])
    return str(issue.get("recommendation") or "Open the issue detail and confirm the affected page.")


def action_validation_label(issue: dict[str, Any]) -> str:
    guidance = issue.get("fix_guidance") or {}
    validation = guidance.get("validation") or []
    if validation:
        return str(validation[0])
    return "Re-run the same audit and confirm the issue count drops."


def build_crawl_events(current_report: Path, site_url: str | None = None) -> list[dict[str, str]]:
    events = []
    for path in lighthouse_json_files(site_url)[-5:]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        events.append(
            {
                "label": "Completed crawl",
                "time": format_lighthouse_fetch_time(str(data.get("fetchTime", "")), path),
                "score": calculate_overview_score(data.get("categories", {})),
                "current": "true" if path == current_report else "false",
            }
        )
    return list(reversed(events))


def extract_lighthouse_scores(categories: dict[str, Any]) -> dict[str, str]:
    labels = {
        "performance": "Performance",
        "accessibility": "Accessibility",
        "best-practices": "Best Practices",
        "seo": "SEO",
        "pwa": "PWA",
    }
    scores = {}
    for key, label in labels.items():
        category = categories.get(key) or {}
        score = category.get("score")
        scores[label] = f"{round(score * 100)}" if isinstance(score, (int, float)) else "n/a"
    return scores


def extract_score_breakdown(categories: dict[str, Any]) -> list[dict[str, str]]:
    labels = {
        "performance": "Performance",
        "accessibility": "Accessibility",
        "best-practices": "Best Practices",
        "seo": "SEO",
    }
    rows = []
    for key, label in labels.items():
        category = categories.get(key) or {}
        score = category.get("score")
        numeric = round(score * 100, 1) if isinstance(score, (int, float)) else None
        rows.append(
            {
                "label": label,
                "score": f"{numeric:g}" if numeric is not None else "n/a",
                "bar": f"{max(0, min(100, numeric or 0)):.0f}",
                "status": score_label(score),
            }
        )
    return rows


def extract_lighthouse_metrics(audits: dict[str, Any]) -> list[dict[str, str]]:
    metric_ids = [
        ("first-contentful-paint", "FCP"),
        ("largest-contentful-paint", "LCP"),
        ("total-blocking-time", "TBT"),
        ("cumulative-layout-shift", "CLS"),
        ("speed-index", "Speed Index"),
    ]
    metrics = []
    for audit_id, label in metric_ids:
        audit = audits.get(audit_id) or {}
        metrics.append(
            {
                "label": label,
                "value": str(audit.get("displayValue") or "n/a"),
                "score": score_label(audit.get("score")),
            }
        )
    return metrics


def extract_lighthouse_opportunities(audits: dict[str, Any]) -> list[dict[str, str]]:
    opportunities = []
    for audit in audits.values():
        details = audit.get("details") or {}
        savings = details.get("overallSavingsMs") or details.get("overallSavingsBytes")
        if audit.get("scoreDisplayMode") != "metricSavings" or not savings:
            continue
        display_value = str(audit.get("displayValue") or "").strip()
        opportunities.append(
            {
                "title": str(audit.get("title") or "Untitled opportunity"),
                "display_value": display_value,
                "score": score_label(audit.get("score")),
                "savings": float(savings),
            }
        )

    opportunities.sort(key=lambda item: item["savings"], reverse=True)
    return [
        {
            "title": item["title"],
            "display_value": item["display_value"] or item["score"],
            "score": item["score"],
        }
        for item in opportunities[:5]
    ]


def extract_lighthouse_issues(
    categories: dict[str, Any], audits: dict[str, Any]
) -> list[dict[str, str]]:
    issue_map: dict[str, dict[str, Any]] = {}
    category_labels = {
        "performance": "Performance",
        "accessibility": "Accessibility",
        "best-practices": "Best Practices",
        "seo": "SEO",
        "pwa": "PWA",
    }

    for category_id, category in categories.items():
        category_label = category_labels.get(category_id, category.get("title", category_id))
        for ref in category.get("auditRefs", []):
            audit_id = ref.get("id")
            audit = audits.get(audit_id or "") or {}
            score = audit.get("score")
            weight = float(ref.get("weight") or 0)
            display_mode = audit.get("scoreDisplayMode")

            if (
                not isinstance(score, (int, float))
                or score >= 0.9
                or display_mode in {"notApplicable", "manual", "informative"}
                or weight <= 0
            ):
                continue

            title = str(audit.get("title") or audit_id or "Untitled issue")
            display_value = str(audit.get("displayValue") or "").strip()
            points = max(0.1, (1 - score) * weight * 2.5)
            details = audit.get("details") or {}
            occurrences = count_detail_items(details)
            role = classify_role(category_label, title, str(audit.get("description") or ""))
            key = str(audit_id or f"{category_label}:{title}")
            recommendation = recommendation_text(category_label, title)
            candidate = {
                "id": str(audit_id or ""),
                "title": title,
                "category": category_label,
                "role": role,
                "responsibility": responsibility_label(role, title),
                "conformance": conformance_label(category_label, title),
                "element": element_label(title, str(audit.get("description") or "")),
                "impact": display_value or f"{occurrences} occurrence(s)",
                "occurrences": str(occurrences),
                "pages": "1",
                "difficulty": difficulty_label(points, score),
                "difficulty_dots": difficulty_dots(points, score),
                "points": points,
                "points_label": f"{points:.1f}",
                "status": score_label(score),
                "recommendation": recommendation,
                "fix_guidance": build_fix_guidance(
                    category_label,
                    title,
                    str(audit_id or ""),
                    role,
                    recommendation,
                    occurrences,
                    points,
                ),
                "affected_examples": extract_affected_examples(details),
            }
            current = issue_map.get(key)
            category_rank = {"Accessibility": 5, "SEO": 4, "Performance": 3, "Best Practices": 2, "PWA": 1}
            if (
                current is None
                or category_rank.get(category_label, 0) > category_rank.get(str(current.get("category")), 0)
                or (
                    category_rank.get(category_label, 0) == category_rank.get(str(current.get("category")), 0)
                    and points > float(current.get("points") or 0)
                )
            ):
                issue_map[key] = candidate

    issues = sorted(issue_map.values(), key=lambda item: item["points"], reverse=True)
    return [
        {
            **item,
            "points": item["points_label"],
        }
        for item in issues[:12]
    ]


def extract_resolved_checks(
    categories: dict[str, Any], audits: dict[str, Any]
) -> list[dict[str, str]]:
    resolved: list[dict[str, Any]] = []
    for category in categories.values():
        category_label = str(category.get("title") or "General")
        for ref in category.get("auditRefs", []):
            audit = audits.get(ref.get("id") or "") or {}
            score = audit.get("score")
            weight = float(ref.get("weight") or 0)
            if score != 1 or weight <= 0:
                continue
            resolved.append(
                {
                    "title": str(audit.get("title") or ref.get("id") or "Passed check"),
                    "category": category_label,
                    "points": weight,
                    "points_label": f"{max(0.1, weight * 0.5):.1f}",
                }
            )

    resolved.sort(key=lambda item: item["points"], reverse=True)
    return [
        {
            "title": item["title"],
            "category": item["category"],
            "points": item["points_label"],
        }
        for item in resolved[:8]
    ]


def count_potential_checks(audits: dict[str, Any]) -> int:
    return sum(
        1
        for audit in audits.values()
        if audit.get("scoreDisplayMode") in {"manual", "informative"}
    )


def group_issues_by_role(issues: list[dict[str, str]]) -> list[dict[str, Any]]:
    roles = ["Editor", "Webmaster", "Developer"]
    grouped = []
    for role in roles:
        role_issues = [issue for issue in issues if issue.get("role") == role]
        points = sum(float(issue.get("points", "0")) for issue in role_issues)
        grouped.append(
            {
                "role": role,
                "issue_count": len(role_issues),
                "points": f"{points:.1f}",
                "items": role_issues[:4],
            }
        )
    return grouped


def select_remediation_issue(issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    for issue in issues:
        if issue.get("affected_examples"):
            return issue
    return issues[0] if issues else None


def find_issue(issues: list[dict[str, Any]], issue_id: str) -> dict[str, Any] | None:
    for issue in issues:
        if issue.get("id") == issue_id:
            return issue
    return None


def extract_page_overview(data: dict[str, Any], audits: dict[str, Any]) -> dict[str, Any]:
    final_url = data.get("finalDisplayedUrl") or data.get("finalUrl") or ""
    title_audit = audits.get("document-title") or {}
    crawlable_audit = audits.get("is-crawlable") or {}
    viewport_audit = audits.get("viewport") or {}
    return {
        "url": final_url,
        "title": str(title_audit.get("displayValue") or "Latest audited page"),
        "status": "Crawlable" if crawlable_audit.get("score") == 1 else "Needs review",
        "viewport": "Configured" if viewport_audit.get("score") == 1 else "Needs review",
        "fetch_time": format_date(str(data.get("fetchTime", ""))),
    }


def extract_pages_with_issues(
    data: dict[str, Any], issues: list[dict[str, str]]
) -> list[dict[str, str]]:
    final_url = data.get("finalDisplayedUrl") or data.get("finalUrl") or ""
    title = final_url.replace("https://", "").replace("http://", "").strip("/") or "Audited page"
    issue_count = len(issues)
    occurrences = sum(int(issue.get("occurrences", "0")) for issue in issues)
    points = sum(float(issue.get("points", "0")) for issue in issues)
    top_categories = []
    for issue in issues:
        category = issue.get("category", "General")
        if category not in top_categories:
            top_categories.append(category)
    return [
        {
            "title": title,
            "url": final_url,
            "issues": str(issue_count),
            "occurrences": str(occurrences),
            "page_level": "1",
            "categories": ", ".join(top_categories[:3]) or "General",
            "points": f"{points:.1f}",
            "status": "Needs review" if issue_count else "Passing",
        }
    ]


def build_policy_summary(issues: list[dict[str, Any]]) -> list[dict[str, str]]:
    policies = [
        {
            "name": "WCAG conformance",
            "description": "Accessibility requirements mapped from Lighthouse audits",
            "matches": [issue for issue in issues if issue.get("category") == "Accessibility"],
        },
        {
            "name": "Core Web Vitals",
            "description": "Performance issues that affect user experience and ranking",
            "matches": [issue for issue in issues if issue.get("category") == "Performance"],
        },
        {
            "name": "Search indexability",
            "description": "SEO and crawlability signals for discoverability",
            "matches": [issue for issue in issues if issue.get("category") == "SEO"],
        },
        {
            "name": "ACT rule mapping",
            "description": "Rule metadata prepared for ACT-style accessibility outcomes",
            "matches": [issue for issue in issues if issue.get("category") == "Accessibility"],
        },
        {
            "name": "Content quality",
            "description": "Editorial checks that can be assigned to content owners",
            "matches": [issue for issue in issues if issue.get("role") == "Editor"],
        },
    ]
    return [
        {
            "name": policy["name"],
            "description": policy["description"],
            "issues": str(len(policy["matches"])),
            "status": "Needs attention" if policy["matches"] else "Passing",
        }
        for policy in policies
    ]


def build_standards_engine_summary(issues: list[dict[str, Any]]) -> dict[str, str]:
    accessibility_issues = [
        issue for issue in issues if issue.get("category") == "Accessibility"
    ]
    return {
        "name": "Alfa-ready standards engine",
        "status": "Model ready",
        "source": "Siteimprove Alfa pattern",
        "rule_model": "WCAG + ACT mapping",
        "report_model": "EARL JSON-LD planned",
        "coverage": f"{len(accessibility_issues)} accessibility finding(s)",
        "description": (
            "Structured so Lighthouse, Pa11y, Oobee, and a future Alfa adapter can feed "
            "one transparent rules and compliance layer."
        ),
    }


def build_capability_matrix(
    issues: list[dict[str, Any]], audits: dict[str, Any], reports: list[dict[str, str]]
) -> list[dict[str, str]]:
    categories = {issue.get("category", "") for issue in issues}
    has_lighthouse = bool(audits)
    has_reports = bool(reports)
    return [
        {
            "area": "Metrics",
            "status": "Live" if has_lighthouse else "Setup",
            "coverage": "Scores, Core Web Vitals, issue counts",
            "next_step": "Add traffic/session analytics when a privacy-safe source is connected",
        },
        {
            "area": "Reporting",
            "status": "Live" if has_reports else "Setup",
            "coverage": "HTML, JSON, history, dashboards",
            "next_step": "Add scheduled PDF/email packs",
        },
        {
            "area": "Monitoring",
            "status": "Partial",
            "coverage": "Scheduled Lighthouse and crawl history",
            "next_step": "Add change alerts and regression detection",
        },
        {
            "area": "Standards",
            "status": "Live" if "Accessibility" in categories or "SEO" in categories else "Setup",
            "coverage": "WCAG, Core Web Vitals, SEO policy mapping",
            "next_step": "Add ACT/EARL exports through an Alfa adapter",
        },
        {
            "area": "SEO",
            "status": "Live" if "SEO" in categories or has_lighthouse else "Setup",
            "coverage": "Metadata, crawlability, Lighthouse SEO",
            "next_step": "Add keyword and competitor modules",
        },
        {
            "area": "Automation",
            "status": "Partial",
            "coverage": "Scripted scans, scheduled collector, APIs",
            "next_step": "Add task assignment and notifications",
        },
        {
            "area": "AI assistance",
            "status": "Partial",
            "coverage": "Issue summaries and remediation guidance",
            "next_step": "Add alt-text and ARIA fix suggestions",
        },
        {
            "area": "Administration",
            "status": "Planned",
            "coverage": "API endpoints and module status",
            "next_step": "Add users, roles, policies, and audit logs",
        },
    ]


def build_integration_summary() -> list[dict[str, str]]:
    return [
        {
            "name": "CMS workflow",
            "status": "Planned",
            "detail": "Slots for WordPress, Sitefinity, Optimizely, or custom CMS plugins",
        },
        {
            "name": "CI/CD",
            "status": "Live",
            "detail": "Lighthouse CI uploads and historical comparison",
        },
        {
            "name": "Crawler stack",
            "status": "Live",
            "detail": "Lighthouse reports, Pa11y dashboard, Oobee deep scan, LinkChecker",
        },
        {
            "name": "Public API",
            "status": "Live",
            "detail": "/api/lighthouse, /api/issues, /api/seo, /api/standards",
        },
    ]


def build_export_formats() -> list[dict[str, str]]:
    return [
        {"name": "HTML report", "status": "Live", "detail": "Human-readable Lighthouse report"},
        {"name": "JSON API", "status": "Live", "detail": "Structured dashboard and issue data"},
        {"name": "EARL JSON-LD", "status": "Planned", "detail": "Accessibility conformance exchange format"},
        {"name": "SARIF", "status": "Planned", "detail": "Developer workflow and code scanning output"},
    ]


def build_feature_modules(
    issues: list[dict[str, Any]], audits: dict[str, Any], reports: list[dict[str, str]]
) -> list[dict[str, str]]:
    report_names = " ".join(report.get("name", "").lower() for report in reports)
    return [
        {
            "name": "Dashboard",
            "status": "Live" if audits else "Setup",
            "detail": "DCI, QA, Accessibility, SEO, Policy, Analytics and report cards",
        },
        {
            "name": "Quality Assurance",
            "status": "Partial",
            "detail": f"{len(issues)} Lighthouse issues; spelling and full broken-link rules are connector-backed",
        },
        {
            "name": "Accessibility",
            "status": "Live" if audits else "Setup",
            "detail": "WCAG-style score breakdown, issues, resolved checks, and issue details",
        },
        {
            "name": "SEO Advanced",
            "status": "Partial",
            "detail": "SEO score, technical/content/mobile/user-experience splits, issues and recommendations",
        },
        {
            "name": "Policy",
            "status": "Partial",
            "detail": "Policy mapping exists; authoring library and custom policy workflows are planned",
        },
        {
            "name": "Marketing Analytics",
            "status": "Partial",
            "detail": "Matomo connector exists; real data appears after site tracking and token setup",
        },
        {
            "name": "Campaigns",
            "status": "Partial",
            "detail": "Matomo UTM campaign tables are wired; URL shortener and email ops are connector-backed",
        },
        {
            "name": "Behaviour maps",
            "status": "Partial",
            "detail": "Top pages and live counters are Matomo-backed; heat/click/scroll maps need a behaviour plugin",
        },
        {
            "name": "Content Quality",
            "status": "Partial",
            "detail": "Lighthouse content/accessibility checks are mapped; spelling and tone need Vale/LanguageTool",
        },
        {
            "name": "Documents and PDFs",
            "status": "Planned",
            "detail": "PDF/Office inventory and document accessibility connector is represented",
        },
        {
            "name": "Email Marketing",
            "status": "Setup",
            "detail": "Mautic service and Vision6-style setup page are available under the marketing profile",
        },
        {
            "name": "Drupal / SDP Publishing",
            "status": "Setup",
            "detail": "Drupal JSON:API connector page is ready for content inventory and prepublish governance",
        },
        {
            "name": "Privacy and Cookies",
            "status": "Partial",
            "detail": "Security/privacy Lighthouse checks are mapped; cookie inventory needs a CMP/crawler connector",
        },
        {
            "name": "Uptime and Response",
            "status": "Partial",
            "detail": "HTTP status and server response signals are mapped; alerts need Uptime Kuma or similar",
        },
        {
            "name": "Prepublish checks",
            "status": "Planned",
            "detail": "CMS/browser-extension workflow is represented, but page overlay editing is not connected",
        },
        {
            "name": "Link integrity",
            "status": "On demand" if "link" in report_names else "Setup",
            "detail": "Run LinkChecker to populate full broken-link reports",
        },
    ]


def build_seo_advanced_summary(
    categories: dict[str, Any], audits: dict[str, Any]
) -> dict[str, Any]:
    seo = score_from_category(categories, "seo")
    performance = score_from_category(categories, "performance")
    accessibility = score_from_category(categories, "accessibility")
    best_practices = score_from_category(categories, "best-practices")
    viewport = audits.get("viewport", {}).get("score")
    mobile_score = 99 if viewport == 1 else 72
    technical = average_known([seo, best_practices])
    content = seo
    user_experience = average_known([accessibility, performance])
    return {
        "overall": score_to_string(seo),
        "tabs": [
            {"name": "Activity Plan overview", "status": "Planned"},
            {"name": "Issues and recommendations", "status": "Live"},
            {"name": "Content optimization", "status": "Partial"},
            {"name": "Duplicate content", "status": "Planned"},
            {"name": "Keyword suggestions", "status": "Starter suggestions"},
            {"name": "Google Search Console", "status": "Connector needed"},
        ],
        "details": [
            {"name": "Technical", "score": score_to_string(technical), "bar": score_bar(technical)},
            {"name": "Content", "score": score_to_string(content), "bar": score_bar(content)},
            {"name": "User experience", "score": score_to_string(user_experience), "bar": score_bar(user_experience)},
            {"name": "Mobile", "score": str(mobile_score), "bar": str(mobile_score)},
        ],
    }


KEYWORD_STOP_WORDS = {
    "www",
    "http",
    "https",
    "html",
    "com",
    "au",
    "org",
    "net",
    "the",
    "and",
    "for",
    "with",
    "from",
    "your",
    "you",
    "our",
    "shop",
    "page",
    "pages",
    "products",
    "product",
    "collections",
    "collection",
    "category",
    "blog",
    "news",
    "home",
    "index",
    "search",
    "cart",
    "account",
    "login",
    "checkout",
    "contact",
    "about",
    "privacy",
    "policy",
    "terms",
    "conditions",
}


def build_keyword_suggestions(
    data: dict[str, Any],
    audits: dict[str, Any],
    issues: list[dict[str, Any]],
    reports: list[dict[str, str]],
) -> dict[str, Any]:
    final_url = str(
        data.get("finalDisplayedUrl") or data.get("finalUrl") or data.get("requestedUrl") or ""
    )
    sitemap = load_latest_sitemap_report(
        [report for report in reports if report.get("kind") == "Sitemap crawl report"]
    )
    sitemap_urls = normalize_sitemap_urls(sitemap)
    source_urls = [final_url] + sitemap_urls
    source_urls = [url for url in source_urls if url]

    page_title = audit_text(audits.get("document-title"))
    meta_description = audit_text(audits.get("meta-description"))
    text_sources = [page_title, meta_description]
    counter: Counter[str] = Counter()
    url_sources: dict[str, set[str]] = {}
    content_keywords = extract_content_keywords(source_urls[:6])
    search_console = build_search_console_summary(final_url)

    for url in source_urls:
        parsed_url = urlparse(url)
        tokens = keyword_tokens(parsed_url.path) or keyword_tokens(parsed_url.netloc)
        for token in tokens:
            counter[token] += 2 if token in keyword_tokens(parsed_url.path) else 1
            url_sources.setdefault(token, set()).add(url)

    for text in text_sources:
        for token in keyword_tokens(text):
            counter[token] += 1

    for item in content_keywords:
        keyword = item["keyword"]
        counter[keyword] += item["weight"]
        for url in item["pages"]:
            url_sources.setdefault(keyword, set()).add(url)

    rows = []
    for keyword, count in counter.most_common(14):
        urls = sorted(url_sources.get(keyword, set()))
        page = urls[0] if urls else final_url
        affected_pages = len(urls) if urls else 1
        score_gain = keyword_score_gain(keyword, count, affected_pages, issues)
        difficulty = keyword_difficulty(keyword, affected_pages, issues)
        metrics = match_search_console_metrics(keyword, page, search_console.get("rows", []))
        rows.append(
            {
                "keyword": keyword,
                "intent": classify_keyword_intent(page, keyword),
                "source_count": str(max(count, 1)),
                "affected_pages": str(affected_pages),
                "score_gain": f"{score_gain:.2f}",
                "difficulty": difficulty,
                "status": "Suggested",
                "opportunity": keyword_opportunity(keyword, issues),
                "page": page,
                "reason": keyword_reason(keyword, page, count, issues, content_keywords),
                "action": keyword_action(keyword, page),
                "clicks": metrics.get("clicks", "-"),
                "impressions": metrics.get("impressions", "-"),
                "ctr": metrics.get("ctr", "-"),
                "position": metrics.get("position", "-"),
            }
        )

    page_opportunities = build_page_keyword_opportunities(source_urls[:40], issues)
    optimization_briefs = build_keyword_optimization_briefs(page_opportunities, rows)
    status = "Generated" if rows else "Needs sitemap"
    source = keyword_source_label(sitemap_urls, content_keywords)
    return {
        "status": status,
        "source": source,
        "count": len(rows),
        "rows": rows,
        "page_opportunities": page_opportunities,
        "optimization_briefs": optimization_briefs,
        "search_console": search_console,
        "tracked_pages": len(source_urls),
        "overview": build_keyword_overview(rows, source_urls, issues),
        "filters": [
            {"name": "All suggestions", "count": str(len(rows)), "active": True},
            {"name": "Highest impact", "count": str(sum(1 for row in rows if float(row["score_gain"]) >= 1.5)), "active": False},
            {"name": "Quick wins", "count": str(sum(1 for row in rows if row["difficulty"] == "Low")), "active": False},
            {"name": "Needs metadata", "count": str(sum(1 for row in rows if "meta" in row["opportunity"].lower())), "active": False},
        ],
        "activity_plan": build_keyword_activity_plan(rows, page_opportunities),
        "content_gaps": build_keyword_content_gaps(audits, issues),
        "engine": {
            "content_extractor": "YAKE" if yake else "Not installed",
            "content_pages_sampled": str(len({url for item in content_keywords for url in item["pages"]})),
            "content_keywords": str(len(content_keywords)),
        },
        "next_integrations": [
            {
                "name": "Google Search Console",
                "status": "Connected" if search_console.get("connected") else "Next",
                "value": "Queries, impressions, clicks, CTR and average position",
            },
            {
                "name": "Keyword rank tracking",
                "status": "Later",
                "value": "Scheduled SERP checks by target phrase and landing page",
            },
            {
                "name": "Content approval workflow",
                "status": "Later",
                "value": "Mark recommendations as accepted, ignored, assigned or fixed",
            },
        ],
        "next_step": (
            "Connect Google Search Console later to replace these starter ideas with real queries, impressions, clicks and ranking movement."
        ),
    }


def normalize_sitemap_urls(sitemap: dict[str, Any] | None) -> list[str]:
    if not sitemap:
        return []
    urls = sitemap.get("urls") or sitemap.get("sample_pages") or []
    normalized = []
    for item in urls:
        if isinstance(item, dict):
            url = str(item.get("loc") or item.get("url") or "")
        else:
            url = str(item)
        if url.startswith(("http://", "https://")):
            normalized.append(url)
    return normalized


def audit_text(audit: Any) -> str:
    if not isinstance(audit, dict):
        return ""
    parts = [
        str(audit.get("title") or ""),
        str(audit.get("description") or ""),
        str(audit.get("displayValue") or ""),
    ]
    return " ".join(parts)


def extract_content_keywords(urls: list[str]) -> list[dict[str, Any]]:
    if not yake or not urls:
        return []

    extractor = yake.KeywordExtractor(lan="en", n=3, dedupLim=0.9, top=14)
    scored: dict[str, dict[str, Any]] = {}
    for url in urls:
        content = fetch_keyword_source_text(url)
        if len(content) < 80:
            continue
        try:
            keywords = extractor.extract_keywords(content)
        except Exception:
            continue
        for phrase, score in keywords:
            normalized = normalize_keyword_phrase(phrase)
            if not normalized:
                continue
            weight = max(1, round((1 / max(score, 0.001)) * 0.08))
            item = scored.setdefault(
                normalized,
                {"keyword": normalized, "weight": 0, "pages": set(), "source": "YAKE"},
            )
            item["weight"] += min(weight, 8)
            item["pages"].add(url)

    rows = []
    for item in scored.values():
        rows.append(
            {
                "keyword": item["keyword"],
                "weight": item["weight"],
                "pages": sorted(item["pages"]),
                "source": item["source"],
            }
        )
    return sorted(rows, key=lambda row: row["weight"], reverse=True)[:18]


def fetch_keyword_source_text(url: str) -> str:
    try:
        response = requests.get(
            url,
            timeout=5,
            headers={"User-Agent": "OpenAuditBot/1.0"},
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException:
        return ""
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        return ""

    soup = BeautifulSoup(response.text, "html.parser")
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    parts = [
        normalize_text(soup.title.string if soup.title else ""),
        meta_content(soup, "description"),
        meta_property(soup, "og:title"),
        meta_property(soup, "og:description"),
    ]
    parts.extend(normalize_text(tag.get_text(" ")) for tag in soup.find_all(["h1", "h2", "h3"])[:18])
    parts.extend(normalize_text(tag.get("alt", "")) for tag in soup.find_all("img")[:20])
    parts.extend(normalize_text(tag.get_text(" ")) for tag in soup.find_all("p")[:24])
    return " ".join(part for part in parts if part)


def normalize_keyword_phrase(value: str) -> str:
    words = keyword_tokens(value)
    if not words:
        return ""
    if len(words) > 4:
        words = words[:4]
    if all(word in KEYWORD_STOP_WORDS for word in words):
        return ""
    return " ".join(words)


def keyword_tokens(value: str) -> list[str]:
    parsed = urlparse(value)
    if parsed.netloc:
        value = f"{parsed.netloc} {parsed.path}"
    value = re.sub(r"[^A-Za-z0-9]+", " ", value).lower()
    tokens = []
    for token in value.split():
        if len(token) < 3 or token.isdigit() or token in KEYWORD_STOP_WORDS:
            continue
        if len(set(token)) <= 1:
            continue
        tokens.append(token)
    return tokens


def keyword_source_label(
    sitemap_urls: list[str], content_keywords: list[dict[str, Any]]
) -> str:
    sources = []
    if content_keywords:
        sources.append("YAKE content extraction")
    if sitemap_urls:
        sources.append("Sitemap")
    sources.append("Lighthouse report")
    return " + ".join(sources)


def build_search_console_summary(site_url: str) -> dict[str, Any]:
    rows = load_search_console_rows(site_url)
    top_queries = sorted(
        rows,
        key=lambda row: (safe_int(row.get("impressions")), safe_int(row.get("clicks"))),
        reverse=True,
    )[:12]
    return {
        "connected": bool(rows),
        "status": "Imported CSV" if rows else "CSV not imported",
        "row_count": str(len(rows)),
        "source": search_console_source_label(site_url, rows),
        "rows": top_queries,
        "next_step": (
            "Export Search Console performance data with Query, Page, Clicks, Impressions, CTR and Position columns, then place it in config/search-console/."
            if not rows
            else "Use these real queries to validate which keyword suggestions deserve content updates first."
        ),
    }


def load_search_console_rows(site_url: str) -> list[dict[str, str]]:
    expected_key = site_key(site_url)
    expected_host = site_label(site_url).lower().replace("www.", "")
    rows: list[dict[str, str]] = []
    for directory in SEARCH_CONSOLE_DIRS:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.csv")):
            file_scoped = expected_key in path.name.lower()
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames:
                continue
            for raw in reader:
                normalized = normalize_search_console_row(raw)
                if not normalized.get("query"):
                    continue
                page = normalized.get("page", "")
                page_host = site_label(page).lower().replace("www.", "") if page else ""
                if page and expected_host and expected_host not in page_host:
                    continue
                if not page and not file_scoped:
                    continue
                normalized["source_file"] = path.name
                rows.append(normalized)
    return rows


def normalize_search_console_row(raw: dict[str, str]) -> dict[str, str]:
    normalized = {normalize_column_name(key): str(value or "").strip() for key, value in raw.items()}
    query = first_present(normalized, ["query", "top_queries", "queries", "keyword", "search_term"])
    page = first_present(normalized, ["page", "top_pages", "pages", "url", "landing_page"])
    clicks = first_present(normalized, ["clicks", "click"])
    impressions = first_present(normalized, ["impressions", "impression"])
    ctr = first_present(normalized, ["ctr", "click_through_rate"])
    position = first_present(normalized, ["position", "average_position", "avg_position"])
    return {
        "query": query,
        "page": page,
        "clicks": normalize_number_text(clicks),
        "impressions": normalize_number_text(impressions),
        "ctr": ctr,
        "position": normalize_position_text(position),
    }


def normalize_column_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def first_present(row: dict[str, str], keys: list[str]) -> str:
    for key in keys:
        if row.get(key):
            return row[key]
    return ""


def normalize_number_text(value: str) -> str:
    value = value.replace(",", "").strip()
    if not value:
        return ""
    try:
        number = float(value)
    except ValueError:
        return value
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def normalize_position_text(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return f"{float(value):.1f}"
    except ValueError:
        return value


def safe_int(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "").replace("%", "")))
    except ValueError:
        return 0


def search_console_source_label(site_url: str, rows: list[dict[str, str]] | None = None) -> str:
    row_files = sorted({row.get("source_file", "") for row in (rows or []) if row.get("source_file")})
    if row_files:
        return ", ".join(row_files)
    expected_key = site_key(site_url)
    matches = []
    for directory in SEARCH_CONSOLE_DIRS:
        if not directory.exists():
            continue
        matches.extend(path.name for path in directory.glob("*.csv") if expected_key in path.name.lower())
    return ", ".join(sorted(matches)) if matches else "No CSV found"


def match_search_console_metrics(
    keyword: str, page: str, rows: list[dict[str, str]]
) -> dict[str, str]:
    if not rows:
        return {}
    keyword_tokens_set = set(keyword_tokens(keyword))
    page_path = urlparse(page).path.lower()
    best: dict[str, str] = {}
    best_score = -1
    for row in rows:
        query = row.get("query", "")
        query_tokens = set(keyword_tokens(query))
        if not query_tokens:
            continue
        score = len(keyword_tokens_set & query_tokens)
        row_page = row.get("page", "")
        if row_page and urlparse(row_page).path.lower() == page_path:
            score += 2
        if query.lower() in keyword.lower() or keyword.lower() in query.lower():
            score += 2
        if score > best_score:
            best_score = score
            best = row
    if best_score <= 0:
        return {}
    return {
        "clicks": best.get("clicks", "") or "-",
        "impressions": best.get("impressions", "") or "-",
        "ctr": best.get("ctr", "") or "-",
        "position": best.get("position", "") or "-",
    }


def classify_keyword_intent(page: str, keyword: str) -> str:
    path = urlparse(page).path.lower()
    if "product" in path or keyword in {"kit", "led", "tray", "strut", "bracket"}:
        return "Product"
    if "collection" in path or "category" in path:
        return "Category"
    if any(term in path for term in ["guide", "blog", "how", "support"]):
        return "Informational"
    return "Landing page"


def keyword_opportunity(keyword: str, issues: list[dict[str, Any]]) -> str:
    issue_text = " ".join(str(issue.get("title") or "").lower() for issue in issues)
    if "meta description" in issue_text:
        return "Add stronger meta description"
    if "document title" in issue_text or "title" in issue_text:
        return "Improve page title"
    if "heading" in issue_text:
        return "Align H1 and headings"
    return "Strengthen copy and internal links"


def keyword_score_gain(
    keyword: str, count: int, affected_pages: int, issues: list[dict[str, Any]]
) -> float:
    issue_bonus = min(len(issues) * 0.05, 0.7)
    page_bonus = min(affected_pages * 0.06, 1.2)
    keyword_bonus = 0.25 if len(keyword) >= 6 else 0.1
    return round(0.35 + page_bonus + issue_bonus + keyword_bonus + min(count * 0.03, 0.5), 2)


def keyword_difficulty(
    keyword: str, affected_pages: int, issues: list[dict[str, Any]]
) -> str:
    if affected_pages <= 2 and len(issues) <= 3:
        return "Low"
    if affected_pages <= 8:
        return "Medium"
    return "High"


def keyword_reason(
    keyword: str,
    page: str,
    count: int,
    issues: list[dict[str, Any]],
    content_keywords: list[dict[str, Any]] | None = None,
) -> str:
    intent = classify_keyword_intent(page, keyword).lower()
    if any(item.get("keyword") == keyword for item in (content_keywords or [])):
        return f"Extracted from live page content with YAKE and mapped to crawled {intent} signals."
    if issues:
        return f"Appears across crawled {intent} signals and overlaps with current SEO issues."
    if count > 3:
        return f"Repeated across crawled {intent} URLs, so it is likely a page theme."
    return f"Found in the audited URL or metadata and worth validating against real search data."


def keyword_action(keyword: str, page: str) -> str:
    label = recommended_keyword_phrase(keyword, page)
    if classify_keyword_intent(page, keyword) == "Product":
        return f"Use '{label}' in the product title, first paragraph, image alt text and related-product links where accurate."
    if classify_keyword_intent(page, keyword) == "Category":
        return f"Use '{label}' in the category intro, title tag, H1 and internal links from related pages."
    return f"Use '{label}' naturally in the page title, H1, meta description and one useful supporting paragraph."


def recommended_keyword_phrase(keyword: str, page: str) -> str:
    path = urlparse(page).path.lower()
    tokens = keyword_tokens(path)
    joined = " ".join(tokens)
    keyword = readable_phrase(keyword_tokens(keyword) or [keyword.replace("-", " ").strip()])

    if "stop" in tokens and "tail" in tokens:
        return "LED stop tail indicator lamps"
    if "worklight" in joined or "work" in tokens:
        return "LED work lights"
    if "lightbar" in joined or "lightbars" in joined:
        return "LED light bars"
    if "marker" in tokens and "reflector" in tokens:
        return "LED marker reflector lights"
    if "black" in tokens and "series" in tokens:
        return "Black Series heavy duty LED lights"
    if "ceiling" in tokens:
        return "LED ceiling lights"
    if "tail" in tokens and "lamp" in tokens:
        return "LED tail lamps"
    if "driving" in tokens and "light" in tokens:
        return "LED driving lights"
    if keyword in {"led", "light", "lamp"} and tokens:
        return readable_phrase(tokens[:4])
    if keyword in {"30v", "12v", "24v"}:
        return f"{keyword.upper()} automotive LED lights"
    if classify_keyword_intent(page, keyword) == "Product" and "led" not in keyword.lower():
        return f"LED {keyword}"
    return readable_phrase(keyword_tokens(keyword) or [keyword])


def readable_phrase(tokens: list[str]) -> str:
    replacements = {
        "ind": "indicator",
        "rev": "reverse",
        "10pk": "10 pack",
        "30v": "10-30V",
        "12v": "12V",
        "24v": "24V",
        "worklight": "work lights",
        "lightbar": "light bar",
    }
    words = [replacements.get(token, token) for token in tokens if token]
    phrase = " ".join(words)
    phrase = phrase.replace("led", "LED")
    return phrase.strip()


def build_keyword_optimization_briefs(
    pages: list[dict[str, str]], rows: list[dict[str, str]]
) -> list[dict[str, str]]:
    row_by_page = {row.get("page", ""): row for row in rows}
    briefs = []
    for item in pages[:6]:
        page = item.get("page", "")
        base_keyword = item.get("suggested_keyword", "")
        keyword = recommended_keyword_phrase(base_keyword, page)
        intent = item.get("intent") or classify_keyword_intent(page, base_keyword)
        page_type = "collection" if intent == "Category" else "product" if intent == "Product" else "landing"
        row = row_by_page.get(page, {})
        briefs.append(
            {
                "page": page,
                "keyword": keyword,
                "intent": intent,
                "priority": item.get("priority", "Review"),
                "points": row.get("score_gain", "Workflow"),
                "clicks": row.get("clicks", ""),
                "impressions": row.get("impressions", ""),
                "ctr": row.get("ctr", ""),
                "position": row.get("position", ""),
                "title": seo_title_example(keyword),
                "h1": seo_h1_example(keyword),
                "meta": seo_meta_example(keyword, page_type),
                "intro": seo_intro_example(keyword, page_type),
                "alt": seo_alt_example(keyword),
                "internal_links": seo_internal_link_example(keyword, page_type),
            }
        )
    return briefs


def seo_title_example(keyword: str) -> str:
    return compact_text(f"{keyword} | TruVision LED", 62)


def seo_h1_example(keyword: str) -> str:
    return keyword


def seo_meta_example(keyword: str, page_type: str) -> str:
    if page_type == "collection":
        text = f"Shop {keyword} for trucks, trailers, caravans and commercial vehicles. Explore durable automotive lighting from TruVision LED."
    elif page_type == "product":
        text = f"View {keyword} with specifications, voltage range and fitment details. Built for reliable automotive and commercial vehicle lighting."
    else:
        text = f"Learn about {keyword}, compare suitable options and find reliable TruVision LED lighting for your vehicle or fleet."
    return compact_text(text, 158)


def seo_intro_example(keyword: str, page_type: str) -> str:
    if page_type == "collection":
        return f"Add a short intro above the product grid explaining who {keyword} are for, common vehicle uses, voltage range, durability and why customers should choose this range."
    if page_type == "product":
        return f"Add one paragraph near the top describing the main use case for this {keyword}, key specifications, installation context and compatible vehicle applications."
    return f"Add helpful copy that explains the buyer problem, compares options and naturally uses '{keyword}' once near the start."


def seo_alt_example(keyword: str) -> str:
    return f"{keyword} product image for automotive and commercial vehicle lighting"


def seo_internal_link_example(keyword: str, page_type: str) -> str:
    if page_type == "collection":
        return f"Link to this page from related product pages using anchor text like '{keyword}' or a close variant."
    return f"Link from related collection pages and compatible accessories using descriptive anchor text that includes '{keyword}'."


def build_keyword_overview(
    rows: list[dict[str, str]], urls: list[str], issues: list[dict[str, Any]]
) -> dict[str, str]:
    total_gain = sum(float(row.get("score_gain", "0") or 0) for row in rows)
    high_impact = sum(1 for row in rows if float(row.get("score_gain", "0") or 0) >= 1.5)
    quick_wins = sum(1 for row in rows if row.get("difficulty") == "Low")
    issue_drag = min(len(issues) * 0.7, 8)
    return {
        "score": f"{max(40, min(94, 68 + high_impact * 1.1 + quick_wins * 1.6 - issue_drag)):.1f}",
        "points_available": f"{total_gain:.1f}",
        "suggestions": str(len(rows)),
        "pages": str(len(urls)),
        "quick_wins": str(quick_wins),
        "issues_used": str(len(issues)),
    }


def build_keyword_activity_plan(
    rows: list[dict[str, str]], pages: list[dict[str, str]]
) -> list[dict[str, str]]:
    top_rows = sorted(
        rows, key=lambda row: float(row.get("score_gain", "0") or 0), reverse=True
    )[:4]
    plan = []
    for index, row in enumerate(top_rows, start=1):
        plan.append(
            {
                "step": str(index),
                "title": f"Review '{row['keyword']}' on its best matching page",
                "owner": "SEO / Content",
                "points": row.get("score_gain", "0"),
                "action": row.get("action", ""),
            }
        )
    if pages:
        plan.append(
            {
                "step": str(len(plan) + 1),
                "title": "Map target phrases to landing pages",
                "owner": "SEO / Content",
                "points": "Workflow",
                "action": "Use the page opportunity list to avoid multiple pages competing for the same phrase.",
            }
        )
    return plan


def build_page_keyword_opportunities(
    urls: list[str], issues: list[dict[str, Any]]
) -> list[dict[str, str]]:
    rows = []
    seen = set()
    for url in urls:
        phrase = suggested_phrase_from_url(url)
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        rows.append(
            {
                "page": url,
                "suggested_keyword": phrase,
                "intent": classify_keyword_intent(url, phrase.split()[0]),
                "priority": page_keyword_priority(url, issues),
                "action": "Check title, H1, meta description, intro copy and internal links for this phrase.",
            }
        )
        if len(rows) >= 8:
            break
    return rows


def suggested_phrase_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return ""
    if path.lower().endswith((".md", ".txt", ".json", ".xml", ".pdf")):
        return ""
    tokens = keyword_tokens(path)
    if not tokens:
        tokens = keyword_tokens(urlparse(url).netloc)
    return " ".join(tokens[:4])


def page_keyword_priority(url: str, issues: list[dict[str, Any]]) -> str:
    issue_text = " ".join(str(issue.get("title") or "").lower() for issue in issues)
    if any(term in issue_text for term in ["meta description", "document title", "heading"]):
        return "High"
    if any(term in urlparse(url).path.lower() for term in ["product", "collection", "category"]):
        return "Medium"
    return "Review"


def build_keyword_content_gaps(
    audits: dict[str, Any], issues: list[dict[str, Any]]
) -> list[dict[str, str]]:
    checks = [
        ("Title tag", audits.get("document-title"), "Put the primary keyword near the start of the title."),
        ("Meta description", audits.get("meta-description"), "Summarize the page benefit and include the primary keyword once."),
        ("Headings", audits.get("heading-order"), "Use one clear H1 and supporting H2s around related topics."),
        ("Image alt text", audits.get("image-alt"), "Describe product or page images with useful, non-spammy wording."),
    ]
    gaps = []
    for name, audit, action in checks:
        score = audit.get("score") if isinstance(audit, dict) else None
        if score != 1:
            gaps.append(
                {
                    "area": name,
                    "status": "Needs review" if score is not None else "Not checked",
                    "action": action,
                }
            )
    if not gaps and issues:
        gaps.append(
            {
                "area": "Content coverage",
                "status": "Review",
                "action": "Compare the suggested terms with the page copy and add missing helpful detail.",
            }
        )
    return gaps


def build_accessibility_breakdown(
    categories: dict[str, Any], issues: list[dict[str, Any]]
) -> list[dict[str, str]]:
    base = score_from_category(categories, "accessibility")
    accessibility_issues = [
        issue for issue in issues if issue.get("category") == "Accessibility"
    ]
    aria_count = sum(1 for issue in accessibility_issues if issue.get("element") == "ARIA")
    return [
        {"name": "Level A", "score": score_to_string(adjust_score(base, 8)), "issues": str(len(accessibility_issues)), "code": "A"},
        {"name": "Level AA", "score": score_to_string(adjust_score(base, 4)), "issues": str(len(accessibility_issues)), "code": "AA"},
        {"name": "Level AAA", "score": score_to_string(adjust_score(base, -6)), "issues": "Manual", "code": "AAA"},
        {"name": "WAI-ARIA authoring practices", "score": score_to_string(adjust_score(base, 2)), "issues": str(aria_count), "code": "ARIA"},
        {"name": "Accessibility best practices", "score": score_to_string(base), "issues": str(len(accessibility_issues)), "code": "SI"},
    ]


def build_prepublish_summary(issues: list[dict[str, Any]]) -> list[dict[str, str]]:
    accessibility = [issue for issue in issues if issue.get("category") == "Accessibility"]
    content = [issue for issue in issues if issue.get("role") == "Editor"]
    policy = [issue for issue in issues if issue.get("category") in {"SEO", "Best Practices"}]
    return [
        {
            "name": "Accessibility",
            "count": str(len(accessibility)),
            "detail": "WCAG, ARIA and screen-reader risks before publishing",
            "status": "Live",
        },
        {
            "name": "Content",
            "count": str(len(content)),
            "detail": "Content, metadata, spelling and broken-link checks",
            "status": "Partial",
        },
        {
            "name": "Policy",
            "count": str(len(policy)),
            "detail": "Policy issues from SEO, best practices and configured rules",
            "status": "Partial",
        },
    ]


def build_analytics_summary(
    audits: dict[str, Any], issues: list[dict[str, Any]], reports: list[dict[str, str]]
) -> dict[str, Any]:
    resource_count = network_resource_count(audits)
    return {
        "visits": "Connector needed",
        "page_views": str(max(resource_count, 0)),
        "interactions": "Connector needed",
        "key_metrics_used": f"{len(issues)} tracked audit issue(s)",
        "reports": str(len(reports)),
        "status": "Needs analytics source",
    }


def build_campaign_summary() -> dict[str, Any]:
    return {
        "status": "Connector needed",
        "monitored_campaigns": "0",
        "unmonitored_campaigns": "Not connected",
        "utm_links": "Not connected",
        "shortener": "Planned",
    }


def build_behavior_summary(audits: dict[str, Any]) -> dict[str, Any]:
    screenshot_score = audits.get("screenshot-thumbnails", {}).get("score")
    return {
        "status": "Connector needed",
        "heat_map": "Planned",
        "click_map": "Planned",
        "scroll_map": "Planned",
        "screenshot": "Available" if screenshot_score is not None else "Needs run data",
    }


def build_content_quality_summary(audits: dict[str, Any]) -> dict[str, Any]:
    rows = [
        content_check("Document title", audits.get("document-title"), "Page has a useful browser/search result title."),
        content_check("Meta description", audits.get("meta-description"), "Page includes a search-friendly description."),
        content_check("Heading structure", audits.get("heading-order"), "Headings follow a meaningful hierarchy."),
        content_check("Link text", audits.get("link-name"), "Links have clear, descriptive names."),
        content_check("Image text alternatives", audits.get("image-alt"), "Images have accessible alternative text."),
        content_check("Readable font sizes", audits.get("font-size"), "Text is legible on mobile and desktop."),
        content_check("HTML language", audits.get("html-has-lang"), "The page language is declared for assistive tech and translation."),
    ]
    issue_count = sum(1 for row in rows if row["status"] != "Passed")
    return {
        "status": "Needs review" if issue_count else "Passed",
        "issue_count": issue_count,
        "checks": rows,
        "connector_note": "Add Vale, LanguageTool or textlint later for spelling, tone and editorial policy checks.",
    }


def content_check(name: str, audit: Any, detail: str) -> dict[str, str]:
    audit = audit or {}
    score = audit.get("score")
    if score == 1:
        status = "Passed"
    elif score in (None, ""):
        status = "Not available"
    else:
        status = "Needs review"
    return {
        "name": name,
        "status": status,
        "detail": str(audit.get("title") or detail),
        "value": str(audit.get("displayValue") or ""),
    }


def build_link_integrity_summary(reports: list[dict[str, str]]) -> dict[str, Any]:
    link_reports = [
        report for report in reports if "link" in report.get("name", "").lower()
    ]
    link_data = load_latest_linkcheck_report(link_reports)
    error_count = int(link_data.get("error_count") or 0) if link_data else 0
    warning_count = int(link_data.get("warning_count") or 0) if link_data else 0
    checked_count = int(link_data.get("checked_count") or 0) if link_data else 0
    return {
        "status": "Needs fixes" if error_count else "Report available" if link_reports else "Run needed",
        "report_count": len(link_reports),
        "reports": link_reports[:6],
        "error_count": str(error_count),
        "warning_count": str(warning_count),
        "checked_count": str(checked_count),
        "sample_errors": (link_data or {}).get("sample_errors", []),
        "checks": [
            {
                "name": "Broken links",
                "status": "Needs fixes" if error_count else "Report available" if link_reports else "Run LinkChecker",
                "detail": f"{error_count} error(s), {warning_count} warning(s), {checked_count} checked link(s).",
            },
            {
                "name": "Redirect chains",
                "status": "Planned",
                "detail": "Use crawler output to detect slow or fragile redirect paths.",
            },
            {
                "name": "Link ownership",
                "status": "Planned",
                "detail": "Group link fixes by page owner, template or content team.",
            },
        ],
    }


def load_latest_linkcheck_report(reports: list[dict[str, str]]) -> dict[str, Any] | None:
    for report in reports:
        name = report.get("name", "")
        path = REPORTS_DIR / name
        if not path.exists() or not name.lower().endswith(".txt"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        return parse_linkcheck_text(text)
    return None


def parse_linkcheck_text(text: str) -> dict[str, Any]:
    error_count = extract_first_int(text, [r"(\d+)\s+errors?\s+found", r"errors?\s*:\s*(\d+)"])
    warning_count = extract_first_int(text, [r"(\d+)\s+warnings?\s+found", r"warnings?\s*:\s*(\d+)"])
    checked_count = extract_first_int(text, [r"(\d+)\s+links?\s+in", r"checked\s+(\d+)\s+links?"])
    sample_errors = []
    for line in text.splitlines():
        lowered = line.lower()
        if "writing to uninitialized or closed file" in lowered:
            continue
        if "urls are still active" in lowered and "after a timeout" in lowered:
            continue
        if any(token in lowered for token in ["error", "404", "403", "500", "timeout", "connection"]):
            sample_errors.append(line.strip())
        if len(sample_errors) >= 8:
            break
    if error_count == 0 and sample_errors:
        error_count = len(sample_errors)
    return {
        "error_count": error_count,
        "warning_count": warning_count,
        "checked_count": checked_count,
        "sample_errors": sample_errors,
    }


def extract_first_int(text: str, patterns: list[str]) -> int:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                return 0
    return 0


def build_document_governance_summary(reports: list[dict[str, str]]) -> dict[str, Any]:
    document_reports = [
        report
        for report in reports
        if any(token in report.get("name", "").lower() for token in ["pdf", "doc", "document"])
    ]
    return {
        "status": "Report available" if document_reports else "Connector needed",
        "report_count": len(document_reports),
        "reports": document_reports[:6],
        "checks": [
            {
                "name": "PDF accessibility",
                "status": "Connector needed",
                "detail": "Add a document crawler or PAC-style workflow for PDF tagging, titles and reading order.",
            },
            {
                "name": "Document freshness",
                "status": "Planned",
                "detail": "Track stale PDFs, ownership and review dates.",
            },
            {
                "name": "Office attachments",
                "status": "Planned",
                "detail": "Inventory DOCX/XLSX/PPTX files and flag risky downloads.",
            },
        ],
    }


def build_privacy_summary(audits: dict[str, Any]) -> dict[str, Any]:
    rows = [
        content_check("HTTPS", audits.get("is-on-https"), "All pages and resources should use HTTPS."),
        content_check("No geolocation prompt on load", audits.get("geolocation-on-start"), "Avoid intrusive permission prompts."),
        content_check("No notification prompt on load", audits.get("notification-on-start"), "Avoid notification prompts before user action."),
        content_check("Security policy", audits.get("csp-xss"), "Use a content security policy to reduce XSS risk."),
        content_check("No paste blocking", audits.get("paste-preventing-inputs"), "Do not block password manager or paste workflows."),
    ]
    return {
        "status": "Needs review" if any(row["status"] == "Needs review" for row in rows) else "Passed",
        "checks": rows,
        "cookie_note": "Cookie consent and privacy inventory need a CMP or crawler connector.",
    }


def build_response_summary(audits: dict[str, Any]) -> dict[str, Any]:
    rows = [
        content_check("HTTP status", audits.get("http-status-code"), "Page returns a successful status code."),
        content_check("Server response time", audits.get("server-response-time"), "Keep initial server latency low."),
        content_check("HTTP to HTTPS redirects", audits.get("redirects-http"), "Redirect HTTP traffic to HTTPS."),
        content_check("HTTP/2 or newer", audits.get("uses-http2"), "Serve requests over modern protocols where possible."),
        content_check("Back/forward cache", audits.get("bf-cache"), "Pages should be eligible for fast browser restores."),
    ]
    return {
        "status": "Needs review" if any(row["status"] == "Needs review" for row in rows) else "Passed",
        "checks": rows,
        "uptime_note": "Add Uptime Kuma later for scheduled uptime checks, incidents and alerts.",
    }


def build_connector_summary(reports: list[dict[str, str]]) -> list[dict[str, str]]:
    report_names = " ".join(report.get("name", "").lower() for report in reports)
    return [
        {"name": "Lighthouse", "status": "Connected", "detail": "Performance, SEO, accessibility and best-practice reports."},
        {"name": "Lighthouse CI", "status": "Connected when token is configured", "detail": "Stores build history and score trends."},
        {"name": "Pa11y", "status": "External dashboard", "detail": "Scheduled accessibility testing and historical results."},
        {"name": "Matomo", "status": "Optional connector", "detail": "Analytics, campaigns and page behaviour."},
        {"name": "Mautic", "status": "Optional connector", "detail": "Vision6-style email marketing and automation."},
        {"name": "Drupal / SDP", "status": "Optional connector", "detail": "Publishing inventory and prepublish governance via JSON:API."},
        {"name": "LinkChecker", "status": "Report available" if "link" in report_names else "Run needed", "detail": "Broken-link crawling and reachability checks."},
        {"name": "Oobee", "status": "Report available" if "oobee" in report_names or "purple" in report_names else "Run needed", "detail": "Deep whole-site accessibility crawler reports."},
    ]


def build_crawler_summary(
    data: dict[str, Any], audits: dict[str, Any], reports: list[dict[str, str]]
) -> dict[str, Any]:
    sitemap_reports = [
        report for report in reports if "sitemap" in report.get("name", "").lower()
    ]
    sitemap_data = load_latest_sitemap_report(sitemap_reports)
    sitemap_url_count = int(sitemap_data.get("url_count") or 0) if sitemap_data else 0
    sitemap_error_count = int(sitemap_data.get("error_count") or 0) if sitemap_data else 0
    sitemap_count = int(sitemap_data.get("sitemap_count") or 0) if sitemap_data else 0
    return {
        "robots": [
            content_check("Robots.txt", audits.get("robots-txt"), "Robots directives are reachable and valid."),
            content_check("Crawlable page", audits.get("is-crawlable"), "Search engines can crawl the page."),
            content_check("Canonical", audits.get("canonical"), "Canonical links are valid and consistent."),
            content_check("Hreflang", audits.get("hreflang"), "Language alternates are valid where used."),
            content_check("HTTP status", audits.get("http-status-code"), "The audited URL returns a healthy status code."),
        ],
        "structured": [
            content_check("Structured data", audits.get("structured-data"), "Schema.org and rich result data validates."),
            content_check("Document title", audits.get("document-title"), "Title can be used in search snippets."),
            content_check("Meta description", audits.get("meta-description"), "Description is available for snippets."),
            content_check("Crawlable anchors", audits.get("crawlable-anchors"), "Links can be followed by crawlers."),
        ],
        "sitemaps": {
            "status": "Report available" if sitemap_reports else "Run needed",
            "reports": sitemap_reports[:6],
            "final_url": data.get("finalDisplayedUrl") or data.get("finalUrl") or "",
            "url_count": str(sitemap_url_count),
            "sitemap_count": str(sitemap_count),
            "error_count": str(sitemap_error_count),
            "sample_pages": (sitemap_data or {}).get("urls", [])[:8],
            "errors": (sitemap_data or {}).get("errors", [])[:5],
            "generated_at": format_date(str((sitemap_data or {}).get("generated_at", ""))),
            "source": (sitemap_data or {}).get("sitemap_entry", ""),
            "next_step": "Use the sitemap pages as the crawl queue for Lighthouse, Pa11y, LinkChecker and content checks.",
        },
    }


def load_latest_sitemap_report(reports: list[dict[str, str]]) -> dict[str, Any] | None:
    if not reports:
        return None
    for report in reports:
        name = report.get("name", "")
        path = REPORTS_DIR / name
        if not path.exists() or not name.lower().endswith(".json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("kind") == "openaudit-sitemap-crawl":
            return data
    return None


def build_architecture_summary(
    data: dict[str, Any], issues: list[dict[str, Any]]
) -> dict[str, Any]:
    final_url = data.get("finalDisplayedUrl") or data.get("finalUrl") or ""
    parsed_host = final_url.replace("https://", "").replace("http://", "").split("/")[0]
    categories: dict[str, int] = {}
    for issue in issues:
        category = str(issue.get("category") or "General")
        categories[category] = categories.get(category, 0) + 1
    rows = [
        {"area": category, "pages": "1", "issues": str(count), "depth": "Latest audited page"}
        for category, count in sorted(categories.items())
    ]
    if not rows:
        rows = [{"area": "Latest audited page", "pages": "1", "issues": "0", "depth": "0"}]
    return {
        "status": "Single page view",
        "host": parsed_host or "Not available",
        "url": final_url,
        "rows": rows,
        "next_step": "Run Oobee or a sitemap crawler to build full site depth, orphan-page and internal-link maps.",
    }


def build_comparison_summary(reports: list[dict[str, str]]) -> dict[str, Any]:
    lighthouse_reports = [
        report for report in reports if report.get("name", "").endswith(".report.json")
    ]
    rows = []
    previous = None
    for report in lighthouse_reports[:6]:
        score = "n/a"
        report_path = REPORTS_DIR / report["name"]
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            score = calculate_overview_score(data.get("categories", {}))
        except (OSError, json.JSONDecodeError):
            pass
        delta = "-"
        if previous not in (None, "n/a") and score != "n/a":
            delta = f"{int(score) - int(previous):+d}"
        rows.append(
            {
                "report": report["name"],
                "updated": report["updated"],
                "score": score,
                "delta": delta,
            }
        )
        previous = score
    return {
        "status": "Ready" if len(rows) > 1 else "Needs more crawls",
        "rows": rows,
        "next_step": "Keep scheduled Lighthouse runs enabled, then compare score, issue and page changes over time.",
    }


def build_ai_recommendations(issues: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for issue in issues[:6]:
        rows.append(
            {
                "title": issue.get("title", "Untitled issue"),
                "owner": issue.get("responsibility", issue.get("role", "Owner")),
                "prompt": f"Explain and propose a safe fix for: {issue.get('title', 'this issue')}. Include WCAG/SEO context and a test plan.",
                "risk": issue.get("difficulty", "Needs review"),
                "href": issue.get("href", f"/issues/{issue.get('id', '')}"),
            }
        )
    return {
        "status": "Ready" if rows else "Needs issues",
        "rows": rows,
        "guardrail": "AI should draft explanations and remediation plans, but fixes still need human review and regression testing.",
    }


def load_matomo_summary(include_live: bool = True) -> dict[str, Any]:
    client = MatomoClient()
    setup_steps = [
        "Start Matomo with docker compose up -d matomo matomo-db.",
        "Open Matomo, create a site, and add its JavaScript tracking code to the audited website.",
        "Create a Matomo auth token and set MATOMO_TOKEN_AUTH in .env.",
        "Set MATOMO_SITE_ID if Matomo created a site ID other than 1.",
    ]
    summary: dict[str, Any] = {
        "configured": client.configured,
        "status": "connected" if client.configured else "setup needed",
        "public_url": client.public_url,
        "site_id": client.site_id,
        "period": "Last 30 days",
        "setup_steps": setup_steps,
        "analytics": {
            "visits": "Not connected",
            "page_views": "Not connected",
            "actions": "Not connected",
            "bounce_rate": "Not connected",
            "avg_time_on_site": "Not connected",
            "status": "Needs Matomo token",
        },
        "campaigns": {
            "status": "Needs Matomo token",
            "monitored_campaigns": "0",
            "top_campaigns": [],
            "utm_links": "Waiting for campaign traffic",
            "shortener": "Use existing UTM builder or connect Mautic/listmonk later",
        },
        "behavior": {
            "status": "Needs Matomo token",
            "top_pages": [],
            "live_visitors": "Not connected",
            "heat_map": "Matomo Heatmap plugin or future connector",
            "click_map": "Matomo Heatmap plugin or future connector",
            "scroll_map": "Matomo Heatmap plugin or future connector",
        },
        "error": "",
    }
    if not client.configured:
        return summary

    visits = client.get("VisitsSummary.get")
    campaigns = client.get("Referrers.getCampaigns")
    pages = client.get("Actions.getPageUrls")
    live = client.get("Live.getCounters", {"lastMinutes": 30}) if include_live else None

    if client.error and visits is None:
        summary["status"] = "offline"
        summary["error"] = client.error
        summary["analytics"]["status"] = "Matomo API not reachable"
        return summary

    summary["analytics"] = format_matomo_visits(visits)
    summary["campaigns"] = format_matomo_campaigns(campaigns)
    summary["behavior"] = format_matomo_behavior(pages, live)
    summary["status"] = "connected"
    return summary


def load_marketing_summary() -> dict[str, Any]:
    public_url = os.getenv("MAUTIC_URL", "http://localhost:8089").rstrip("/")
    return {
        "status": "setup needed",
        "public_url": public_url,
        "platform": "Mautic",
        "vision6_match": [
            {
                "capability": "Email campaigns",
                "status": "Available after Mautic setup",
                "detail": "Create reusable email campaigns and landing-page forms.",
            },
            {
                "capability": "Segments and contacts",
                "status": "Available after Mautic setup",
                "detail": "Manage audiences, tags, suppression and contact history.",
            },
            {
                "capability": "Automation journeys",
                "status": "Available after Mautic setup",
                "detail": "Build nurture flows and triggered campaign sequences.",
            },
            {
                "capability": "Campaign analytics",
                "status": "Pair with Matomo",
                "detail": "Use UTM tracking to connect email clicks back to OpenAudit analytics.",
            },
        ],
        "setup_steps": [
            "Start Mautic with docker compose --profile marketing up -d mautic mautic-db.",
            "Open Mautic and create the administrator account.",
            "Configure email transport such as SMTP before sending campaigns.",
            "Use UTM links so Matomo can report campaign traffic in OpenAudit.",
        ],
        "recommended_for": "Vision6-style open-source email marketing, forms, segments and automation.",
    }


def load_publishing_summary() -> dict[str, Any]:
    base_url = os.getenv("DRUPAL_BASE_URL", "").strip().rstrip("/")
    jsonapi_path = os.getenv("DRUPAL_JSONAPI_PATH", "/jsonapi").strip() or "/jsonapi"
    summary: dict[str, Any] = {
        "configured": bool(base_url),
        "status": "setup needed" if not base_url else "checking",
        "base_url": base_url,
        "jsonapi_url": f"{base_url}{jsonapi_path}" if base_url else "",
        "platform": "Drupal / SDP",
        "content_types": [],
        "checks": [
            {
                "name": "Publishing inventory",
                "status": "Needs Drupal JSON:API",
                "detail": "Reads content entities, bundles and content freshness signals.",
            },
            {
                "name": "Metadata readiness",
                "status": "Planned",
                "detail": "Connect page title, description and canonical checks to Drupal content.",
            },
            {
                "name": "Prepublish governance",
                "status": "Planned",
                "detail": "Use OpenAudit issues before publishing, similar to Siteimprove CMS workflow.",
            },
            {
                "name": "Victorian SDP fit",
                "status": "Connector pattern ready",
                "detail": "Designed around Drupal-based publishing and JSON:API-style content access.",
            },
        ],
        "setup_steps": [
            "Enable Drupal JSON:API on the publishing site or staging site.",
            "Set DRUPAL_BASE_URL in .env, for example https://www.example.vic.gov.au.",
            "Keep DRUPAL_JSONAPI_PATH as /jsonapi unless the site uses a custom path.",
            "Restart portal and open this page again.",
        ],
        "error": "",
    }
    if not base_url:
        return summary

    try:
        response = requests.get(
            f"{base_url}{jsonapi_path}",
            timeout=3,
            headers={"User-Agent": "OpenAuditBot/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        summary["status"] = "offline"
        summary["error"] = str(exc)
        return summary

    content_types = extract_drupal_jsonapi_resources(payload)
    summary["status"] = "connected"
    summary["content_types"] = content_types
    summary["checks"][0]["status"] = "Connected" if content_types else "Connected no resources listed"
    return summary


def extract_drupal_jsonapi_resources(payload: Any) -> list[dict[str, str]]:
    links = payload.get("links", {}) if isinstance(payload, dict) else {}
    rows = []
    if isinstance(links, dict):
        for key, value in links.items():
            if not isinstance(value, dict) or not key.startswith("node--"):
                continue
            rows.append(
                {
                    "type": key.replace("node--", "Node: "),
                    "href": str(value.get("href") or ""),
                    "status": "Available",
                }
            )
    return rows[:12]


class MatomoClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("MATOMO_API_URL", "http://matomo").rstrip("/")
        self.public_url = os.getenv("MATOMO_URL", "http://localhost:8088").rstrip("/")
        self.site_id = os.getenv("MATOMO_SITE_ID", "1")
        self.token = os.getenv("MATOMO_TOKEN_AUTH", "").strip()
        self.configured = bool(self.token)
        self.error = ""

    def get(self, method: str, extra: dict[str, Any] | None = None) -> Any:
        if not self.configured:
            return None
        params: dict[str, Any] = {
            "module": "API",
            "method": method,
            "idSite": self.site_id,
            "period": "range",
            "date": "last30",
            "format": "JSON",
            "token_auth": self.token,
        }
        if extra:
            params.update(extra)
        try:
            response = requests.get(
                f"{self.base_url}/index.php",
                params=params,
                timeout=2.5,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("result") == "error":
                self.error = str(payload.get("message", "Matomo API returned an error"))
                return None
            return payload
        except (requests.RequestException, ValueError) as exc:
            self.error = str(exc)
            return None


def format_matomo_visits(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        return {
            "visits": "No data",
            "page_views": "No data",
            "actions": "No data",
            "bounce_rate": "No data",
            "avg_time_on_site": "No data",
            "status": "Connected, waiting for traffic",
        }
    return {
        "visits": format_number(data.get("nb_visits")),
        "page_views": format_number(data.get("nb_pageviews")),
        "actions": format_number(data.get("nb_actions")),
        "bounce_rate": format_percent(data.get("bounce_rate")),
        "avg_time_on_site": format_duration(data.get("avg_time_on_site")),
        "status": "Connected",
    }


def format_matomo_campaigns(data: Any) -> dict[str, Any]:
    rows = data if isinstance(data, list) else []
    campaigns = [
        {
            "name": str(row.get("label") or "Unnamed campaign"),
            "visits": format_number(row.get("nb_visits")),
            "actions": format_number(row.get("nb_actions")),
            "bounce_rate": format_percent(row.get("bounce_rate")),
        }
        for row in rows[:8]
        if isinstance(row, dict)
    ]
    return {
        "status": "Connected" if campaigns else "Connected, waiting for campaign traffic",
        "monitored_campaigns": str(len(campaigns)),
        "top_campaigns": campaigns,
        "utm_links": "Detected from Matomo campaign reports" if campaigns else "Waiting for utm_campaign traffic",
        "shortener": "Use Mautic/listmonk later if you need email campaign operations",
    }


def format_matomo_behavior(pages: Any, live: Any) -> dict[str, Any]:
    rows = pages if isinstance(pages, list) else []
    top_pages = [
        {
            "page": str(row.get("label") or "Untitled page"),
            "views": format_number(row.get("nb_hits")),
            "unique_views": format_number(row.get("nb_visits")),
            "avg_time": format_duration(row.get("avg_time_on_page")),
        }
        for row in rows[:8]
        if isinstance(row, dict)
    ]
    live_visitors = "No live data"
    if isinstance(live, dict):
        live_visitors = format_number(live.get("visitors") or live.get("visits"))
    return {
        "status": "Connected" if top_pages else "Connected, waiting for page data",
        "top_pages": top_pages,
        "live_visitors": live_visitors,
        "heat_map": "Matomo Heatmap plugin or future connector",
        "click_map": "Matomo Heatmap plugin or future connector",
        "scroll_map": "Matomo Heatmap plugin or future connector",
    }


def format_number(value: Any) -> str:
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return "0"


def format_percent(value: Any) -> str:
    if value in (None, ""):
        return "0%"
    if isinstance(value, str) and value.endswith("%"):
        return value
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return str(value)


def format_duration(value: Any) -> str:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return "0s"
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {remaining_minutes}m"
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def score_from_category(categories: dict[str, Any], category_id: str) -> float | None:
    score = categories.get(category_id, {}).get("score")
    if isinstance(score, (int, float)):
        return float(score) * 100
    return None


def average_known(values: list[float | None]) -> float | None:
    known = [value for value in values if isinstance(value, (int, float))]
    if not known:
        return None
    return sum(known) / len(known)


def adjust_score(value: float | None, delta: float) -> float | None:
    if value is None:
        return None
    return max(0, min(100, value + delta))


def score_to_string(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def score_bar(value: float | None) -> str:
    if value is None:
        return "0"
    return f"{max(0, min(100, value)):.0f}"


def network_resource_count(audits: dict[str, Any]) -> int:
    resource_audit = audits.get("network-requests") or {}
    details = resource_audit.get("details") or {}
    items = details.get("items") if isinstance(details.get("items"), list) else []
    return len(items)


def build_inventory_summary(data: dict[str, Any], audits: dict[str, Any]) -> list[dict[str, str]]:
    resource_audit = audits.get("network-requests") or {}
    details = resource_audit.get("details") or {}
    items = details.get("items") if isinstance(details.get("items"), list) else []
    resource_count = len(items)
    transfer_size = sum(
        int(item.get("transferSize") or 0)
        for item in items
        if isinstance(item, dict)
    )
    final_url = data.get("finalDisplayedUrl") or data.get("finalUrl") or ""
    return [
        {"label": "Audited pages", "value": "1", "detail": final_url},
        {"label": "Network resources", "value": str(resource_count), "detail": "Requests observed during Lighthouse run"},
        {"label": "Transfer size", "value": format_size(transfer_size), "detail": "Approximate transferred bytes"},
    ]


def count_detail_items(details: dict[str, Any]) -> int:
    items = details.get("items")
    if isinstance(items, list):
        return len(items)
    headings = details.get("headings")
    if isinstance(headings, list):
        return len(headings)
    return 1


def extract_affected_examples(details: dict[str, Any]) -> list[dict[str, str]]:
    examples = []
    items = details.get("items")
    if not isinstance(items, list):
        return examples

    for item in items[:3]:
        node = item.get("node") or item.get("source") or item.get("relatedNode") or {}
        if not isinstance(node, dict):
            continue
        selector = str(node.get("selector") or node.get("nodeLabel") or "Unknown element")
        snippet = str(node.get("snippet") or "").strip()
        explanation = str(node.get("explanation") or item.get("wastedBytes") or "").strip()
        examples.append(
            {
                "selector": compact_text(selector, 140),
                "snippet": compact_text(snippet, 180),
                "explanation": compact_text(explanation, 220),
            }
        )
    return [
        item
        for item in examples
        if item["selector"] != "Unknown element" or item["snippet"] or item["explanation"]
    ]


def compact_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}..."


def classify_role(category: str, title: str, description: str) -> str:
    text = f"{category} {title} {description}".lower()
    category_name = category.lower()

    if category_name == "performance":
        return "Developer"

    if category_name == "seo":
        editor_seo_terms = ["document title", "meta description", "heading", "content"]
        if any(term in text for term in editor_seo_terms):
            return "Editor"
        return "Webmaster"

    developer_accessibility_terms = [
        "aria",
        "focusable",
        "landmark",
        "list",
        "role",
        "id attribute",
    ]
    if any(term in text for term in developer_accessibility_terms):
        return "Developer"

    editor_terms = [
        "title",
        "heading",
        "description",
        "alt",
        "link text",
        "discernible name",
        "contrast",
        "content",
        "tap targets",
    ]
    webmaster_terms = [
        "canonical",
        "robots",
        "http",
        "https",
        "redirect",
        "crawl",
        "index",
        "sitemap",
    ]
    if any(term in text for term in editor_terms):
        return "Editor"
    if any(term in text for term in webmaster_terms):
        return "Webmaster"
    return "Developer"


def responsibility_label(role: str, title: str) -> str:
    if role == "Editor":
        if "contrast" in title.lower():
            return "Visual design"
        return "Content writing"
    if role == "Webmaster":
        return "Technical SEO"
    return "Development"


def conformance_label(category: str, title: str) -> str:
    text = title.lower()
    if category == "Accessibility":
        if "contrast" in text or "aria" in text or "focusable" in text:
            return "AA"
        return "A"
    if category == "SEO":
        return "SEO"
    if category == "Performance":
        return "CWV"
    return "BP"


def element_label(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    element_terms = [
        ("image", "Images"),
        ("link", "Links"),
        ("button", "Controls"),
        ("aria", "ARIA"),
        ("list", "Lists"),
        ("heading", "Headings"),
        ("font", "Fonts"),
        ("script", "JavaScript"),
        ("image", "Images"),
        ("layout", "Layout"),
        ("paint", "Rendering"),
        ("crawl", "Crawl"),
    ]
    for term, label in element_terms:
        if term in text:
            return label
    return "Other"


def recommendation_text(category: str, title: str) -> str:
    text = title.lower()
    if "largest contentful paint" in text:
        return "Optimize the LCP image, preload critical assets, and reduce render-blocking work."
    if "cumulative layout shift" in text:
        return "Reserve dimensions for images, banners, and injected content to prevent layout movement."
    if "contrast" in text:
        return "Increase foreground/background contrast until it meets WCAG AA thresholds."
    if "alt" in text and "image" in text:
        return "Add meaningful alt text to informative images, or empty alt text to decorative images."
    if "aria" in text:
        return "Review ARIA roles and hidden focusable elements in the affected component."
    if "discernible name" in text:
        return "Add visible text, aria-label, or accessible names to links and controls."
    if "crawlable" in text:
        return "Replace JavaScript-only links with crawlable href values where possible."
    if category == "Performance":
        return "Review the Lighthouse opportunity details and address the highest weighted bottleneck first."
    if category == "SEO":
        return "Check indexability, metadata, and crawl paths for the affected page."
    return "Open the full Lighthouse report for examples and affected nodes."


def build_fix_guidance(
    category: str,
    title: str,
    audit_id: str,
    role: str,
    recommendation: str,
    occurrences: int,
    points: float,
) -> dict[str, Any]:
    text = f"{title} {audit_id}".lower()
    guidance = {
        "summary": recommendation,
        "why_it_matters": "This issue reduces the page quality score and can affect users, search engines, or conversion paths.",
        "priority": "High" if points >= 3 else "Medium" if points >= 1 else "Low",
        "owner": responsibility_label(role, title),
        "handoff_note": "Create one ticket for the affected template or component, then re-run Lighthouse after the fix ships.",
        "code_hint": "Open the full Lighthouse report to inspect the exact node, selector, and audit evidence.",
        "where_to_change": [
            {
                "place": "Website source or CMS template",
                "detail": "Find the page, component, theme, or template that renders this element.",
            },
            {
                "place": "Shared component first",
                "detail": "If the issue appears more than once, fix the reusable component instead of one page at a time.",
            },
        ],
        "what_to_change": [
            "Use the evidence section to identify the affected page or selector.",
            "Apply the recommended fix in the source website, not inside OpenAudit.",
            "Publish the website change, then run a new scan.",
        ],
        "success_signal": "The same issue disappears or the occurrence count drops in the next Lighthouse scan.",
        "steps": [
            "Open the affected page and confirm the issue in the full Lighthouse report.",
            "Fix the shared template or component first so repeat occurrences are solved together.",
            "Check the page visually on desktop and mobile after the change.",
            "Re-run the audit and compare the score, occurrence count, and affected examples.",
        ],
        "validation": [
            "The issue no longer appears in Lighthouse for the same URL.",
            "No new accessibility, SEO, or layout regression is introduced.",
            "The updated page still matches the intended content and design.",
        ],
        "acceptance_criteria": [
            "Occurrence count drops from the current finding.",
            "The affected component has a clear owner and repeatable fix.",
            "Evidence is captured in the latest report before closing the task.",
        ],
    }

    if "largest contentful paint" in text or "lcp" in text:
        guidance.update(
            {
                "why_it_matters": "The main content is taking too long to appear, so users may leave before the page feels usable.",
                "handoff_note": "Assign to frontend/performance owner; start with the hero image, critical CSS, and blocking scripts.",
                "code_hint": "Look for the LCP element in Lighthouse, then optimize that exact image, heading block, or hero section.",
                "where_to_change": [
                    {
                        "place": "Hero image or above-the-fold section",
                        "detail": "Usually this is the homepage banner, first product image, heading block, or large background image.",
                    },
                    {
                        "place": "Theme/front-end assets",
                        "detail": "Check image size, CSS, fonts, JavaScript bundles, and third-party scripts loaded before the hero appears.",
                    },
                    {
                        "place": "Hosting/CDN settings",
                        "detail": "If the server is slow, check caching, compression, image CDN, and response time.",
                    },
                ],
                "what_to_change": [
                    "Resize and compress the largest above-the-fold image.",
                    "Serve WebP/AVIF where possible and avoid loading a huge desktop image on mobile.",
                    "Preload only the real hero image or critical font.",
                    "Defer scripts that are not needed before the first screen is visible.",
                ],
                "success_signal": "The next scan shows LCP closer to 2.5 seconds or below, and the Performance score improves.",
                "steps": [
                    "Identify the LCP element in the Lighthouse report.",
                    "Compress and resize the hero image; use WebP/AVIF when suitable.",
                    "Preload the LCP image or critical font only when it is truly above the fold.",
                    "Defer non-critical scripts and remove render-blocking CSS where possible.",
                    "Check server response time and cache headers for the page.",
                ],
                "validation": [
                    "LCP is close to or below 2.5 seconds on mobile.",
                    "The hero area still looks sharp and stable.",
                    "The latest report shows improved Performance score and LCP timing.",
                ],
            }
        )
    elif "cumulative layout shift" in text or "layout shift" in text or "cls" in text:
        guidance.update(
            {
                "why_it_matters": "Unexpected page movement makes people click the wrong thing and creates a poor reading experience.",
                "handoff_note": "Assign to frontend owner; inspect images, ads, banners, cookie notices, and injected widgets.",
                "code_hint": "Reserve width/height or aspect-ratio for media and allocate space before dynamic content loads.",
                "where_to_change": [
                    {
                        "place": "Page template and media components",
                        "detail": "Look at images, videos, embeds, announcement bars, cookie banners, and dynamic sections.",
                    },
                    {
                        "place": "CSS/layout rules",
                        "detail": "Reserve space before assets load by using width, height, min-height, or aspect-ratio.",
                    },
                ],
                "what_to_change": [
                    "Add fixed dimensions or aspect-ratio to images and embeds.",
                    "Reserve space for banners and widgets before they appear.",
                    "Avoid inserting new content above existing content after load.",
                ],
                "success_signal": "The next scan shows CLS close to 0.1 or below and fewer layout shift events.",
                "steps": [
                    "Find the elements listed under layout shifts in the report.",
                    "Add explicit dimensions or aspect-ratio to images, embeds, and video blocks.",
                    "Reserve space for banners, cookie prompts, alerts, and third-party widgets.",
                    "Avoid inserting content above existing content after initial render.",
                ],
                "validation": [
                    "CLS is close to or below 0.1.",
                    "No visible jump occurs during page load on mobile.",
                    "The latest report shows fewer layout shift events.",
                ],
            }
        )
    elif "contrast" in text:
        guidance.update(
            {
                "why_it_matters": "Low contrast can make text unreadable for low-vision users and fails WCAG readability expectations.",
                "handoff_note": "Assign to design system/content owner if this comes from a reusable color token.",
                "code_hint": "Update the foreground/background color pair, not just one page instance, when the style is shared.",
                "where_to_change": [
                    {
                        "place": "Design system or theme CSS",
                        "detail": "Find the color token, button style, card style, link style, or text style used by the affected element.",
                    },
                    {
                        "place": "CMS content style",
                        "detail": "If a content editor picked the color manually, update the content block or restrict unsafe colors.",
                    },
                ],
                "what_to_change": [
                    "Increase contrast between text and background.",
                    "Prefer changing shared color tokens so all repeated uses are fixed.",
                    "Check hover, focus, disabled, and mobile states too.",
                ],
                "success_signal": "The affected text reaches WCAG AA contrast and the contrast issue disappears from the next scan.",
                "steps": [
                    "Open the affected selector and identify the text/background color pair.",
                    "Adjust the color token or component style to meet WCAG AA contrast.",
                    "Check hover, focus, disabled, and mobile states for the same component.",
                    "Avoid solving contrast by only increasing font weight unless the ratio also passes.",
                ],
                "validation": [
                    "Normal text reaches at least 4.5:1 contrast; large text reaches at least 3:1.",
                    "The issue disappears from the Accessibility report.",
                    "The updated color still fits the brand palette.",
                ],
            }
        )
    elif "alt" in text and "image" in text:
        guidance.update(
            {
                "why_it_matters": "Screen reader users cannot understand the purpose of an image when informative images have no alt text.",
                "handoff_note": "Assign to content or theme owner; fix the Shopify section/component that renders these repeated benefit icons.",
                "code_hint": "Find the img tag with class img-benefits-icon and add alt text in the template or image/content field.",
                "where_to_change": [
                    {
                        "place": "Shopify theme section or snippet",
                        "detail": "Look for the section that renders benefit icons using class img-benefits-icon.",
                    },
                    {
                        "place": "Shopify image/content fields",
                        "detail": "If the icons are managed through the theme editor, add alt text on the image/block settings.",
                    },
                ],
                "what_to_change": [
                    "Add an alt attribute to each affected image.",
                    "Use meaningful alt text if the image communicates information.",
                    "Use alt=\"\" if the image is purely decorative and the nearby text already explains it.",
                    "Avoid file names such as benefits-01-shaft.png as alt text.",
                ],
                "success_signal": "The next scan shows zero image-alt occurrences for these benefit icons.",
                "steps": [
                    "Open the Shopify theme file or section that outputs img-benefits-icon.",
                    "Decide whether each icon is informative or decorative.",
                    "Add a useful alt value for informative icons, or alt=\"\" for decorative icons.",
                    "Preview the page and confirm the visible layout did not change.",
                    "Re-run Lighthouse and confirm the image-alt issue disappears.",
                ],
                "validation": [
                    "Every affected img tag has an alt attribute.",
                    "Alt text describes the image purpose, not the file name.",
                    "Decorative icons use empty alt text so screen readers skip them.",
                ],
            }
        )
    elif "aria" in text or "role" in text:
        guidance.update(
            {
                "why_it_matters": "Incorrect ARIA can confuse screen readers and keyboard users even when the page looks fine visually.",
                "handoff_note": "Assign to frontend owner; prefer native HTML before adding ARIA.",
                "code_hint": "Remove invalid ARIA first, then add only the attributes required by the component pattern.",
                "where_to_change": [
                    {
                        "place": "Interactive component code",
                        "detail": "Look at menus, accordions, tabs, modals, buttons, form controls, and custom widgets.",
                    },
                    {
                        "place": "Template markup",
                        "detail": "Prefer native HTML elements before adding ARIA attributes.",
                    },
                ],
                "what_to_change": [
                    "Replace fake clickable divs/spans with buttons or links where possible.",
                    "Remove ARIA attributes that do not match the element role.",
                    "Add only the ARIA attributes required by the interaction pattern.",
                ],
                "success_signal": "Keyboard use and screen reader labels work correctly, and the ARIA audit passes.",
                "steps": [
                    "Inspect the exact node from the audit details.",
                    "Replace custom controls with native buttons, links, inputs, or landmarks where possible.",
                    "Remove ARIA attributes that do not match the element role.",
                    "Check keyboard focus order and screen reader announcement.",
                ],
                "validation": [
                    "The component works with keyboard only.",
                    "Screen reader output describes the control accurately.",
                    "The ARIA-related audit passes in the next scan.",
                ],
            }
        )
    elif "discernible name" in text or "accessible name" in text or "button-name" in text or "link-name" in text:
        guidance.update(
            {
                "why_it_matters": "Unnamed controls are invisible to assistive technology, so users cannot understand what action they trigger.",
                "handoff_note": "Assign to frontend/content owner; visible labels are preferred over hidden labels.",
                "code_hint": "Use clear visible text, aria-label, aria-labelledby, or alt text depending on the element.",
                "where_to_change": [
                    {
                        "place": "Button, link, image, or form component",
                        "detail": "Find the icon-only control, empty link, missing image alt text, or unlabeled input.",
                    },
                    {
                        "place": "CMS content fields",
                        "detail": "If the element comes from content, update image alt text, link text, or form label in the CMS.",
                    },
                ],
                "what_to_change": [
                    "Add visible text where possible.",
                    "For icon-only controls, add a clear aria-label.",
                    "For images, add meaningful alt text or mark decorative images empty.",
                    "Avoid labels like 'click here', 'more', or 'read more' without context.",
                ],
                "success_signal": "Every affected control has a useful accessible name and the audit passes.",
                "steps": [
                    "Find each unnamed button, link, image, or form control in the examples.",
                    "Add a concise label that describes the action or destination.",
                    "Avoid vague labels such as 'click here', 'more', or icon-only controls without a name.",
                    "Check duplicated labels when multiple controls appear on the same page.",
                ],
                "validation": [
                    "Each control has a unique and useful accessible name.",
                    "Keyboard and screen reader users can understand the action.",
                    "The accessible-name audit passes in Lighthouse.",
                ],
            }
        )
    elif "crawlable" in text or "robots" in text or "index" in text or "canonical" in text:
        guidance.update(
            {
                "why_it_matters": "Search engines need crawlable paths and clear index signals to discover and rank important pages.",
                "handoff_note": "Assign to SEO/frontend owner; confirm whether the page should be indexed before changing rules.",
                "code_hint": "Use real href links, valid canonical URLs, and consistent robots directives.",
                "where_to_change": [
                    {
                        "place": "Navigation/template markup",
                        "detail": "Check internal links, menus, pagination, and product/category links.",
                    },
                    {
                        "place": "SEO settings",
                        "detail": "Check robots meta, canonical URL, sitemap inclusion, and redirects.",
                    },
                ],
                "what_to_change": [
                    "Use real anchor href links for important pages.",
                    "Remove conflicting noindex/canonical rules when the page should rank.",
                    "Add important pages to the sitemap and internal navigation.",
                ],
                "success_signal": "The page is crawlable, index signals are consistent, and the SEO audit passes.",
                "steps": [
                    "Confirm whether the affected page should be indexed.",
                    "Replace JavaScript-only navigation with real anchor href values where possible.",
                    "Check canonical, robots meta, sitemap inclusion, and redirect behavior.",
                    "Make sure important internal links are visible in the rendered HTML.",
                ],
                "validation": [
                    "Lighthouse SEO audit passes for crawlability/indexability.",
                    "The page appears in the sitemap if it is meant to be indexed.",
                    "Canonical and robots rules do not conflict.",
                ],
            }
        )
    elif "meta description" in text or "document title" in text or "title" in text:
        guidance.update(
            {
                "why_it_matters": "Clear metadata helps search engines and users understand the page before they visit it.",
                "handoff_note": "Assign to SEO/content owner; fix the CMS template if many pages share the same pattern.",
                "code_hint": "Generate unique title and meta description fields from reliable page content.",
                "where_to_change": [
                    {
                        "place": "CMS SEO fields",
                        "detail": "Edit the page title, meta description, Open Graph title, and description fields.",
                    },
                    {
                        "place": "SEO template rules",
                        "detail": "If many pages have the same issue, update the title/meta template in the CMS or theme.",
                    },
                ],
                "what_to_change": [
                    "Write one unique title for the page.",
                    "Write a useful meta description that matches the visible page content.",
                    "Avoid duplicate metadata across many pages.",
                ],
                "success_signal": "The next scan detects one unique title and useful meta description.",
                "steps": [
                    "Check whether the page has a unique title and meta description.",
                    "Write a concise title that includes the page purpose and brand where appropriate.",
                    "Write a page-specific description that matches the visible content.",
                    "Update CMS field rules if the issue repeats across many pages.",
                ],
                "validation": [
                    "The page has one unique title and one useful meta description.",
                    "Metadata matches the visible page content.",
                    "The SEO metadata audit passes in the next report.",
                ],
            }
        )
    elif category == "Performance":
        guidance.update(
            {
                "why_it_matters": "Performance issues slow down real user journeys and can reduce conversions, engagement, and search quality.",
                "handoff_note": "Assign to frontend/performance owner; start with the highest weighted Lighthouse opportunity.",
                "code_hint": "Compare the audit details with network waterfall, bundle size, images, fonts, and third-party scripts.",
            }
        )
    elif category == "SEO":
        guidance.update(
            {
                "why_it_matters": "SEO issues make it harder for search engines to understand, crawl, and present the page correctly.",
                "handoff_note": "Assign to SEO/content owner; confirm intent before changing index or canonical rules.",
                "code_hint": "Check rendered HTML, metadata, sitemap, robots, canonical, headings, and internal links.",
            }
        )
    elif category == "Accessibility":
        guidance.update(
            {
                "why_it_matters": "Accessibility issues block people using keyboards, screen readers, zoom, or other assistive technology.",
                "handoff_note": "Assign to frontend/content owner; fix shared components before one-off page fixes.",
                "code_hint": "Use semantic HTML first, then add ARIA only when native HTML cannot express the interaction.",
            }
        )

    if occurrences > 1:
        guidance["acceptance_criteria"][0] = f"Occurrence count drops from {occurrences} to zero or has a documented exception."
    return guidance


def build_pa11y_guidance(issue: dict[str, Any]) -> dict[str, Any]:
    code = str(issue.get("audit_id") or issue.get("issue_key") or "").lower()
    title = str(issue.get("title") or "").lower()
    text = f"{code} {title}"
    guidance = {
        "summary": "Use the affected URL, selector, and HTML context to update the source component, then run the same Pa11y scan again.",
        "where": "Website template, CMS component, or page content that renders the affected selector.",
        "owner": str(issue.get("owner") or "Accessibility / Development"),
        "steps": [
            "Open the affected URL and locate the saved selector in browser developer tools.",
            "Fix the reusable component or template before editing individual pages.",
            "Check the result with keyboard and screen-reader-friendly markup.",
            "Run Accessibility (Pa11y) again and confirm the finding is resolved.",
        ],
        "bad_example": "<!-- Inspect the saved HTML context for the failing element. -->",
        "good_example": "<!-- Apply the semantic HTML or accessible attribute required by this WCAG check. -->",
        "verify": "The Pa11y finding disappears for the same page and no new WCAG error is introduced.",
    }

    if any(token in text for token in ["h37", "image-alt", "missing alt", "alt attribute"]):
        guidance.update(
            {
                "summary": "Add meaningful alternative text to informative images and an empty alt attribute to decorative images.",
                "where": "Image field, media component, product card, banner, or CMS image template.",
                "owner": "Content / Development",
                "steps": [
                    "Decide whether the image communicates information or is purely decorative.",
                    "For informative images, describe the purpose in a short alt value without writing 'image of'.",
                    "For decorative images, use alt=\"\" so screen readers skip them.",
                    "Fix the shared image component if the same selector appears on multiple pages.",
                ],
                "bad_example": '<img src="warning-light.jpg">',
                "good_example": '<img src="warning-light.jpg" alt="Amber LED warning light mounted on a truck">',
                "verify": "Every affected img has an appropriate alt value and Pa11y no longer reports the H37 finding.",
            }
        )
    elif any(token in text for token in ["g18", "g145", "contrast", "colour contrast", "color contrast"]):
        guidance.update(
            {
                "summary": "Increase foreground and background contrast to meet WCAG AA: 4.5:1 for normal text and 3:1 for large text.",
                "where": "Design tokens, theme CSS, button styles, text links, banners, or component colour settings.",
                "owner": "Design / Development",
                "steps": [
                    "Inspect the failing element and identify its computed text and background colours.",
                    "Adjust the shared colour token rather than overriding one page when possible.",
                    "Check hover, focus, disabled, and visited states as well as the default state.",
                    "Confirm the new pair meets WCAG AA with a contrast checker.",
                ],
                "bad_example": ".notice { color: #9aa3ad; background: #ffffff; }",
                "good_example": ".notice { color: #475467; background: #ffffff; }",
                "verify": "The computed colour pair meets the required ratio and the Pa11y contrast finding disappears.",
            }
        )
    elif any(token in text for token in ["h44", "h65", "label", "form control"]):
        guidance.update(
            {
                "summary": "Give every form control a persistent, programmatically associated label.",
                "where": "Form builder, search component, checkout field, newsletter form, or CMS form template.",
                "owner": "Development / Content",
                "steps": [
                    "Give the input a stable id.",
                    "Add a visible label whose for value matches that id.",
                    "Do not use placeholder text as the only label.",
                    "Test that clicking the label focuses the control.",
                ],
                "bad_example": '<input type="email" placeholder="Email address">',
                "good_example": '<label for="email">Email address</label>\n<input id="email" type="email" autocomplete="email">',
                "verify": "The control has an accessible name, the visible label focuses it, and Pa11y no longer reports H44/H65.",
            }
        )
    elif any(token in text for token in ["link-name", "discernible", "empty link", "button name"]):
        guidance.update(
            {
                "summary": "Give links and buttons a clear accessible name that describes their destination or action.",
                "where": "Icon button, card link, navigation component, carousel control, or social link.",
                "owner": "Development / Content",
                "steps": [
                    "Use visible descriptive text when the design allows it.",
                    "For icon-only controls, add aria-label or visually hidden text.",
                    "Avoid repeated vague labels such as 'click here' or 'read more' without context.",
                    "Confirm the accessible name in the browser accessibility tree.",
                ],
                "bad_example": '<a href="/cart"><svg aria-hidden="true">...</svg></a>',
                "good_example": '<a href="/cart" aria-label="View shopping cart"><svg aria-hidden="true">...</svg></a>',
                "verify": "Screen readers announce a useful name and Pa11y no longer reports the unnamed control.",
            }
        )
    elif any(token in text for token in ["h42", "heading", "header hierarchy"]):
        guidance.update(
            {
                "summary": "Use semantic heading elements in a logical hierarchy without choosing levels for visual size alone.",
                "where": "Page template, content editor, card component, accordion, or section heading.",
                "owner": "Content / Development",
                "steps": [
                    "Ensure the page has one descriptive h1 for its main topic.",
                    "Use h2 for major sections and h3 for subsections beneath them.",
                    "Move visual styling into CSS instead of selecting a heading level for appearance.",
                    "Review the headings outline before publishing.",
                ],
                "bad_example": '<div class="heading-large">Product range</div>',
                "good_example": '<h2 class="heading-large">Product range</h2>',
                "verify": "The headings outline is logical and Pa11y no longer reports the heading structure finding.",
            }
        )
    elif any(token in text for token in ["h57", "html lang", "language"]):
        guidance.update(
            {
                "summary": "Declare the primary page language on the html element so assistive technology uses the correct pronunciation rules.",
                "where": "Global document template or CMS theme html element.",
                "owner": "Development",
                "steps": [
                    "Set lang on the root html element using a valid language code.",
                    "Use lang attributes on passages that switch to another language.",
                    "Apply the fix in the global layout so every page receives it.",
                ],
                "bad_example": "<html>",
                "good_example": '<html lang="en-AU">',
                "verify": "The document has a valid lang value and Pa11y no longer reports H57.",
            }
        )
    elif any(token in text for token in ["f77", "duplicate id", "ids are not unique"]):
        guidance.update(
            {
                "summary": "Make every HTML id unique within the page and update labels, ARIA references, and fragment links that use it.",
                "where": "Repeated component, modal, accordion, form field, or server-side loop template.",
                "owner": "Development",
                "steps": [
                    "Search the rendered DOM for every occurrence of the duplicate id.",
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

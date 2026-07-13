from __future__ import annotations

import os
import json
import re
import csv
import io
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
        get_crawl_page,
        get_scan_job,
        get_website,
        init_database,
        keyword_action_key,
        list_content_actions,
        list_keyword_actions,
        list_issues as database_issues,
        list_issues_for_page,
        list_crawl_pages,
        list_scan_jobs,
        list_websites,
        reconcile_issues,
        update_issue_status,
        upsert_content_action,
        upsert_keyword_action,
        update_scan_job,
        update_website,
    )
except ImportError:
    from database import (
        create_scan_job,
        create_website,
        database_is_ready,
        delete_website,
        get_crawl_page,
        get_scan_job,
        get_website,
        init_database,
        keyword_action_key,
        list_content_actions,
        list_keyword_actions,
        list_issues as database_issues,
        list_issues_for_page,
        list_crawl_pages,
        list_scan_jobs,
        list_websites,
        reconcile_issues,
        update_issue_status,
        upsert_content_action,
        upsert_keyword_action,
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
MAX_SEARCH_CONSOLE_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_SEARCH_CONSOLE_ROWS = 20_000
_LIGHTHOUSE_SUMMARY_CACHE: dict[str, Any] = {}
_PAGE_HTML_CACHE: dict[str, tuple[float, str]] = {}
SCAN_TYPES = {"full", "accessibility", "content", "lighthouse"}


def validated_scan_type(value: str) -> str:
    scan_type = str(value or "full").strip().lower()
    if scan_type not in SCAN_TYPES:
        raise ValueError(f"Invalid scan type. Choose one of: {', '.join(sorted(SCAN_TYPES))}.")
    return scan_type


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

    @app.get("/pages/inspect")
    def page_inspector():
        sites = load_site_options()
        selected_site = select_site(sites)
        selected_site_key = str(selected_site.get("key") or "")
        page_url = request.args.get("url", "").strip()
        if not selected_site_key or not page_url:
            abort(404)
        crawl_page = get_crawl_page(selected_site_key, page_url)
        if not crawl_page:
            abort(404)
        page_issues = list_issues_for_page(selected_site_key, page_url)
        context = portal_template_context(active_slug="pages")
        return render_template(
            "page_inspector.html",
            crawl_page=crawl_page,
            page_issues=page_issues,
            open_issue_count=sum(1 for issue in page_issues if issue.get("status") not in {"resolved", "ignored"}),
            **context,
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
                        "budget_performance": request.form.get("budget_performance", "70"),
                        "budget_accessibility": request.form.get("budget_accessibility", "80"),
                        "budget_seo": request.form.get("budget_seo", "80"),
                        "budget_lcp_ms": request.form.get("budget_lcp_ms", "2500"),
                        "budget_cls": request.form.get("budget_cls", "0.1"),
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
                "budget_performance": request.form.get("budget_performance", "70"),
                "budget_accessibility": request.form.get("budget_accessibility", "80"),
                "budget_seo": request.form.get("budget_seo", "80"),
                "budget_lcp_ms": request.form.get("budget_lcp_ms", "2500"),
                "budget_cls": request.form.get("budget_cls", "0.1"),
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
                scan_type = validated_scan_type(scan_type)
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
            scan_type = validated_scan_type(str(payload.get("scan_type") or "full"))
            job = create_scan_job(str(payload.get("website_key") or ""), scan_type)
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
        target = activity_plan_return_target(request.form.get("return_to", ""), target)
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
        summary = load_lighthouse_report_summary(site, include_keywords=False)
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
        summary = load_lighthouse_report_summary(site, include_keywords=False)
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
        summary = load_lighthouse_report_summary(site, include_keywords=False)
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
        summary = load_lighthouse_report_summary(site, include_keywords=False)
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
        summary = load_lighthouse_report_summary(site, include_keywords=True)
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

    @app.post("/api/search-console/import")
    def search_console_import():
        requested_key = str(request.form.get("site") or "").strip()
        selected_site = next(
            (site for site in load_site_options() if site.get("key") == requested_key),
            None,
        )
        target = f"/modules/keyword-suggestions?site={quote(requested_key, safe='')}"
        if not selected_site:
            return redirect(f"{target}&gsc=error&message={quote('Choose a valid website first.')}")

        upload = request.files.get("search_console_csv")
        if not upload or not str(upload.filename or "").lower().endswith(".csv"):
            return redirect(f"{target}&gsc=error&message={quote('Choose a Google Search Console CSV file.')}")

        try:
            result = import_search_console_csv(upload, selected_site)
        except ValueError as exc:
            message = compact_text(str(exc), 180)
            return redirect(f"{target}&gsc=error&message={quote(message)}")

        _LIGHTHOUSE_SUMMARY_CACHE.clear()
        return redirect(
            f"{target}&gsc=imported&rows={result['rows']}&skipped={result['skipped']}"
        )

    @app.route("/api/keyword-actions", methods=["GET", "POST"])
    def keyword_actions_api():
        if request.method == "GET":
            site = select_site(load_site_options())
            site_key_value = str(site.get("key") or "")
            return jsonify(list_keyword_actions(site_key_value) if site_key_value else [])

        payload = request.get_json(silent=True) if request.is_json else request.form
        payload = payload or {}
        site_key_value = str(payload.get("site") or "").strip()
        selected_site = next(
            (site for site in load_site_options() if site.get("key") == site_key_value),
            None,
        )
        target = f"/modules/keyword-suggestions?site={quote(site_key_value, safe='')}"
        target = activity_plan_return_target(str(payload.get("return_to") or ""), target)
        if not selected_site:
            message = "Choose a valid website first."
            if request.is_json:
                return jsonify({"error": message}), 400
            return redirect(f"{target}&workflow=error&message={quote(message)}")

        page_url = str(payload.get("page_url") or "").strip()
        expected_host = site_label(str(selected_site.get("url") or "")).lower().replace("www.", "")
        page_host = site_label(page_url).lower().replace("www.", "") if page_url else ""
        if not page_host or page_host != expected_host:
            message = "The task page must belong to the selected website."
            if request.is_json:
                return jsonify({"error": message}), 400
            return redirect(f"{target}&workflow=error&message={quote(message)}")

        try:
            action = upsert_keyword_action(
                site_key_value,
                page_url,
                str(payload.get("keyword") or ""),
                str(payload.get("decision") or "Improve existing page"),
                str(payload.get("status") or "suggested"),
                str(payload.get("owner") or ""),
                str(payload.get("note") or ""),
            )
        except ValueError as exc:
            if request.is_json:
                return jsonify({"error": str(exc)}), 400
            return redirect(f"{target}&workflow=error&message={quote(compact_text(str(exc), 180))}")

        _LIGHTHOUSE_SUMMARY_CACHE.clear()
        if request.is_json:
            return jsonify(action)
        return redirect(f"{target}&workflow=saved")

    @app.route("/api/content-actions", methods=["GET", "POST"])
    def content_actions_api():
        payload = request.get_json(silent=True) if request.is_json else request.form
        payload = payload or {}
        site_key_value = str(payload.get("site") or request.args.get("site") or "").strip()
        if request.method == "GET":
            return jsonify(list_content_actions(site_key_value) if site_key_value else [])
        selected = next((site for site in load_site_options() if site.get("key") == site_key_value), None)
        action_type = str(payload.get("action_type") or "duplicate-content")
        module_slug = "content-optimization" if action_type == "content-optimization" else "duplicate-content"
        target = activity_plan_return_target(str(payload.get("return_to") or ""), f"/modules/{module_slug}?site={quote(site_key_value, safe='')}")
        if not selected:
            return jsonify({"error": "Choose a valid website first."}), 400
        primary_url = str(payload.get("primary_url") or "").strip()
        if site_key(primary_url) != site_key(str(selected.get("url") or "")):
            return jsonify({"error": "The primary page must belong to the selected website."}), 400
        affected_urls = list(dict.fromkeys(url.strip() for url in str(payload.get("affected_urls") or "").split("|") if url.strip()))[:100]
        selected_key = site_key(str(selected.get("url") or ""))
        if any(site_key(url) != selected_key for url in affected_urls):
            return jsonify({"error": "Every affected page must belong to the selected website."}), 400
        try:
            action = upsert_content_action(
                site_key_value, action_type,
                str(payload.get("title") or "Resolve duplicate content"), primary_url,
                affected_urls, str(payload.get("status") or "suggested"),
                str(payload.get("owner") or ""), str(payload.get("note") or ""),
                float(payload.get("points") or 0),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if request.is_json:
            return jsonify(action)
        return redirect(f"{target}&workflow=saved")

    @app.get("/api/activity-plans")
    def activity_plans_api():
        selected_site = select_site(load_site_options())
        selected_site_key = str(selected_site.get("key") or "")
        selected_site_url = str(selected_site.get("url") or "")
        if not selected_site_key or not selected_site_url:
            return jsonify({"rows": [], "total": 0})
        summary = load_lighthouse_report_summary(selected_site_url)
        center = build_activity_plan_center(
            summary.get("keyword_suggestions", {}).get("page_edit_queue", []),
            database_issues(selected_site_key),
            request.args.get("status", "all").strip(),
            list_keyword_actions(selected_site_key),
            list_content_actions(selected_site_key),
        )
        return jsonify(center)

    @app.get("/api/activity-plans/export.csv")
    def activity_plans_export():
        selected_site = select_site(load_site_options())
        selected_site_key = str(selected_site.get("key") or "")
        selected_site_url = str(selected_site.get("url") or "")
        if not selected_site_key or not selected_site_url:
            return Response("No website selected.\n", status=400, mimetype="text/plain")
        summary = load_lighthouse_report_summary(selected_site_url)
        center = build_activity_plan_center(
            summary.get("keyword_suggestions", {}).get("page_edit_queue", []),
            database_issues(selected_site_key), "all",
            list_keyword_actions(selected_site_key), list_content_actions(selected_site_key),
        )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["website", "work_type", "title", "status", "priority", "points", "owner", "page", "note", "updated"])
        for row in center["rows"]:
            writer.writerow([csv_cell(value) for value in (
                selected_site.get("label") or site_label(selected_site_url), row.get("kind", ""),
                row.get("title", ""), row.get("status", ""), row.get("priority", ""),
                row.get("points", ""), row.get("owner", ""), row.get("page_url", ""),
                row.get("note", ""), row.get("updated_at", ""),
            )])
        filename = f"activity-plan-{selected_site_key}.csv"
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})

    @app.get("/api/content-optimization")
    def content_optimization_api():
        selected_site = select_site(load_site_options())
        selected_site_key = str(selected_site.get("key") or "")
        pages = list_crawl_pages(selected_site_key) if selected_site_key else []
        return jsonify(build_content_optimization_summary(pages))

    @app.get("/api/duplicate-content")
    def duplicate_content_api():
        selected_site = select_site(load_site_options())
        selected_site_key = str(selected_site.get("key") or "")
        pages = list_crawl_pages(selected_site_key) if selected_site_key else []
        return jsonify(build_duplicate_content_summary(pages))

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
        summary = load_lighthouse_report_summary(site, include_keywords=False)
        issue = find_issue(summary.get("issues_to_fix", []), issue_id)
        if not issue and site:
            issue = normalize_issue_detail(find_issue(database_issues(site_key(site)), issue_id))
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
            issue_context=build_issue_context_summary(issue, summary, site),
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
    all_websites = list_websites()
    managed_issues = database_issues(selected_site_key) if selected_site_key else []
    crawl_pages = list_crawl_pages(selected_site_key) if selected_site_key else []
    content_optimization = build_content_optimization_summary(crawl_pages)
    duplicate_content = build_duplicate_content_summary(crawl_pages)
    recent_jobs = list_scan_jobs(selected_site_key or None)[:4]
    latest_job = recent_jobs[0] if recent_jobs else None
    lighthouse_summary = load_lighthouse_report_summary(
        selected_site_url,
        include_keywords=active_slug in {"keyword-suggestions", "activity-plans"},
        crawl_pages=crawl_pages,
    )
    activity_plan_center = build_activity_plan_center(
        lighthouse_summary.get("keyword_suggestions", {}).get("page_edit_queue", []),
        managed_issues,
        request.args.get("status", "todo").strip(),
        list_keyword_actions(selected_site_key) if selected_site_key else [],
        list_content_actions(selected_site_key) if selected_site_key else [],
    )
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
        "selected_site_label": selected_site.get("label") or site_label(selected_site_url),
        "site_options": sites,
        "website_count": len(all_websites),
        "active_website_count": sum(1 for website in all_websites if website.get("active")),
        "targets": [site["url"] for site in sites if site.get("url")],
        "reports": load_reports(selected_site_url),
        "crawl_pages": crawl_pages,
        "content_optimization": content_optimization,
        "duplicate_content": duplicate_content,
        "technical_crawl": build_technical_crawl_summary(crawl_pages, managed_issues),
        "ecommerce_summary": build_ecommerce_summary(crawl_pages, managed_issues),
        "wordpress_summary": build_wordpress_summary(crawl_pages, managed_issues),
        "budget_summary": build_budget_summary(selected_site, managed_issues, lighthouse_summary),
        "managed_issues": managed_issues,
        "content_governance": build_content_governance_summary(crawl_pages, managed_issues, selected_site_key),
        "pa11y_issues": [
            {**issue, "guidance": build_pa11y_guidance(issue)}
            for issue in managed_issues
            if issue.get("source") == "pa11y"
        ],
        "lighthouse": lighthouse_summary,
        "activity_plan_center": activity_plan_center,
        "scan_jobs": recent_jobs,
        "latest_scan_job": latest_job,
        "operations_snapshot": build_operations_snapshot(
            selected_site,
            crawl_pages,
            managed_issues,
            recent_jobs,
            lighthouse_summary,
            all_websites,
        ),
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
                {"title": "Performance budgets", "slug": "performance-budgets"},
                {"title": "Security headers", "slug": "security-headers"},
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
                {"title": "Activity plans", "slug": "activity-plans"},
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
            "group": "eCommerce",
            "slug": "ecommerce",
            "badge": "Shopify",
            "items": [
                {"title": "Shopify readiness", "slug": "ecommerce-readiness"},
                {"title": "Conversion tracking", "slug": "conversion-tracking"},
            ],
        },
        {
            "group": "WordPress",
            "slug": "wordpress",
            "badge": "CMS",
            "items": [
                {"title": "WordPress readiness", "slug": "wordpress-readiness"},
                {"title": "Launch checklist", "slug": "wordpress-launch-checklist"},
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


def build_operations_snapshot(
    selected_site: dict[str, str],
    crawl_pages: list[dict[str, Any]],
    managed_issues: list[dict[str, Any]],
    recent_jobs: list[dict[str, Any]],
    lighthouse_summary: dict[str, Any],
    all_websites: list[dict[str, Any]],
) -> dict[str, Any]:
    open_issues = [
        issue for issue in managed_issues if issue.get("status") not in {"resolved", "ignored"}
    ]
    latest_job = recent_jobs[0] if recent_jobs else None
    selected_label = selected_site.get("label") or site_label(selected_site.get("url", ""))
    selected_url = selected_site.get("url", "")
    next_action = "Run the first scan for this website."
    if latest_job:
        status = str(latest_job.get("status") or "")
        if status in {"queued", "running"}:
            next_action = "Wait for the current scan to finish, then review the updated issue list."
        elif status == "failed":
            next_action = "Review the latest scan error, then rerun the scan."
        else:
            next_action = "Open issues and work through the top priority items from the latest scan."
    elif open_issues:
        next_action = "Open issues and start with the highest-point fix."

    report_site = str(
        lighthouse_summary.get("report_source", {}).get("url")
        or lighthouse_summary.get("final_url")
        or ""
    )
    report_matches_selected = True
    if selected_url and report_site:
        report_matches_selected = site_key(selected_url) == site_key(report_site)

    return {
        "selected_label": selected_label or "No website selected",
        "selected_url": selected_url,
        "website_count": len(all_websites),
        "active_website_count": sum(1 for website in all_websites if website.get("active")),
        "page_count": len(crawl_pages),
        "open_issue_count": len(open_issues),
        "resolved_issue_count": sum(
            1 for issue in managed_issues if issue.get("status") == "resolved"
        ),
        "latest_scan_status": str((latest_job or {}).get("status") or "not started").replace("_", " "),
        "latest_scan_message": str((latest_job or {}).get("message") or "No scan has been queued yet."),
        "latest_scan_type": str((latest_job or {}).get("scan_type") or "full"),
        "latest_scan_progress": int((latest_job or {}).get("progress") or 0),
        "latest_scan_started": str((latest_job or {}).get("started_at") or (latest_job or {}).get("created_at") or ""),
        "latest_score": lighthouse_summary.get("overview_score", "n/a"),
        "next_action": next_action,
        "report_matches_selected": report_matches_selected,
        "report_site": report_site,
    }


def build_budget_summary(website: dict[str, Any], issues: list[dict[str, Any]],
                         lighthouse: dict[str, Any]) -> dict[str, Any]:
    budget_issues = [
        issue for issue in issues
        if issue.get("source") == "budget" and issue.get("status") not in {"resolved", "ignored"}
    ]
    failed_ids = {str(issue.get("audit_id") or "") for issue in budget_issues}
    metrics = {str(metric.get("label")): str(metric.get("value") or "n/a") for metric in lighthouse.get("metrics", [])}
    scores = lighthouse.get("scores", {})
    targets = [
        {"id": "budget-performance", "label": "Performance", "current": scores.get("Performance", "n/a"), "target": f">= {website.get('budget_performance', 70)}/100"},
        {"id": "budget-accessibility", "label": "Accessibility", "current": scores.get("Accessibility", "n/a"), "target": f">= {website.get('budget_accessibility', 80)}/100"},
        {"id": "budget-seo", "label": "SEO", "current": scores.get("SEO", "n/a"), "target": f">= {website.get('budget_seo', 80)}/100"},
        {"id": "budget-largest-contentful-paint", "label": "LCP", "current": metrics.get("LCP", "n/a"), "target": f"<= {website.get('budget_lcp_ms', 2500)}ms"},
        {"id": "budget-cumulative-layout-shift", "label": "CLS", "current": metrics.get("CLS", "n/a"), "target": f"<= {website.get('budget_cls', 0.1)}"},
    ]
    for target in targets:
        target["status"] = "Failed" if target["id"] in failed_ids else "Passed" if target["current"] != "n/a" else "Awaiting scan"
    return {
        "targets": targets, "issues": budget_issues,
        "failed": len(budget_issues),
        "passed": sum(1 for target in targets if target["status"] == "Passed"),
        "status": "Action required" if budget_issues else "Within budget" if lighthouse.get("ok") else "Run Lighthouse",
    }


def build_activity_plan_center(
    keyword_queue: list[dict[str, Any]],
    managed_issues: list[dict[str, Any]],
    selected_status: str = "todo",
    saved_keyword_actions: list[dict[str, Any]] | None = None,
    content_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    visible_action_keys: set[str] = set()
    for item in keyword_queue:
        workflow = item.get("workflow") or {}
        status = str(workflow.get("status") or "suggested")
        if workflow.get("action_key"):
            visible_action_keys.add(str(workflow["action_key"]))
        rows.append(
            {
                "kind": "keyword",
                "title": str(item.get("keyword") or "Keyword recommendation"),
                "subtitle": str(item.get("decision_label") or item.get("decision") or "SEO recommendation"),
                "page_url": str(item.get("page") or ""),
                "status": status,
                "bucket": activity_status_bucket(status),
                "owner": str(workflow.get("owner") or "Unassigned"),
                "note": str(workflow.get("note") or ""),
                "priority": str(item.get("priority") or "Review"),
                "points": str(item.get("points") or "0"),
                "updated_at": str(workflow.get("updated_at") or ""),
                "decision": str(item.get("decision") or "Improve existing page"),
                "keyword": str(item.get("keyword") or ""),
            }
        )

    for action in saved_keyword_actions or []:
        action_key_value = str(action.get("action_key") or "")
        if action_key_value in visible_action_keys:
            continue
        status = str(action.get("status") or "suggested")
        rows.append(
            {
                "kind": "keyword",
                "title": str(action.get("keyword") or "Keyword recommendation"),
                "subtitle": str(action.get("decision") or "SEO recommendation"),
                "page_url": str(action.get("page_url") or ""),
                "status": status,
                "bucket": activity_status_bucket(status),
                "owner": str(action.get("owner") or "Unassigned"),
                "note": str(action.get("note") or ""),
                "priority": "Tracked",
                "points": "-",
                "updated_at": str(action.get("updated_at") or ""),
                "decision": str(action.get("decision") or "Improve existing page"),
                "keyword": str(action.get("keyword") or ""),
            }
        )

    for issue in managed_issues:
        status = str(issue.get("status") or "open")
        rows.append(
            {
                "kind": "issue",
                "title": str(issue.get("title") or "Audit issue"),
                "subtitle": f"{issue.get('source', 'audit')}: {issue.get('category', 'Issue')}",
                "page_url": "",
                "status": status,
                "bucket": activity_status_bucket(status),
                "owner": str(issue.get("owner") or "Unassigned"),
                "note": str(issue.get("ignored_reason") or ""),
                "priority": str(issue.get("priority") or "medium").title(),
                "points": str(issue.get("points") or "0"),
                "updated_at": str(issue.get("updated_at") or ""),
                "issue_id": str(issue.get("id") or ""),
                "issue_key": str(issue.get("issue_key") or issue.get("audit_id") or ""),
            }
        )

    for action in content_actions or []:
        status = str(action.get("status") or "suggested")
        rows.append({
            "kind": "content", "title": str(action.get("title") or "Content governance action"),
            "subtitle": (
                f"Content optimization: {len(action.get('affected_urls') or [])} page(s) to improve"
                if action.get("action_type") == "content-optimization"
                else f"Duplicate content: {len(action.get('affected_urls') or [])} page(s) to review"
            ),
            "page_url": str(action.get("primary_url") or ""), "status": status,
            "bucket": activity_status_bucket(status), "owner": str(action.get("owner") or "Unassigned"),
            "note": str(action.get("note") or ""), "priority": "High",
            "points": str(action.get("points") or "0"), "updated_at": str(action.get("updated_at") or ""),
            "action_type": str(action.get("action_type") or "duplicate-content"),
            "affected_urls": action.get("affected_urls") or [],
        })

    allowed_filters = {"all", "todo", "in_progress", "completed", "ignored"}
    selected_status = selected_status if selected_status in allowed_filters else "todo"
    counts = Counter(row["bucket"] for row in rows)
    visible = rows if selected_status == "all" else [row for row in rows if row["bucket"] == selected_status]
    priority_order = {"highest impact": 0, "high": 1, "quick win": 1, "medium": 2, "review": 3, "low": 4}
    visible.sort(
        key=lambda row: (
            0 if row["bucket"] == "in_progress" else 1,
            priority_order.get(str(row["priority"]).lower(), 3),
            str(row.get("title") or "").lower(),
        )
    )
    filters = [
        {"key": "all", "label": "All", "count": len(rows)},
        {"key": "todo", "label": "To do", "count": counts.get("todo", 0)},
        {"key": "in_progress", "label": "In progress", "count": counts.get("in_progress", 0)},
        {"key": "completed", "label": "Completed", "count": counts.get("completed", 0)},
        {"key": "ignored", "label": "Ignored", "count": counts.get("ignored", 0)},
    ]
    actionable = counts.get("todo", 0) + counts.get("in_progress", 0)
    completion_rate = round((counts.get("completed", 0) / len(rows)) * 100) if rows else 0
    owner_counts = Counter(str(row.get("owner") or "Unassigned") for row in rows if row["bucket"] not in {"completed", "ignored"})
    type_counts = Counter(str(row.get("kind") or "issue") for row in rows)
    return {
        "rows": visible,
        "filters": filters,
        "selected_status": selected_status,
        "total": len(rows),
        "actionable": actionable,
        "in_progress": counts.get("in_progress", 0),
        "completed": counts.get("completed", 0),
        "completion_rate": completion_rate,
        "owner_workload": [{"owner": owner, "count": count} for owner, count in owner_counts.most_common(8)],
        "work_types": [
            {"key": key, "label": {"keyword": "Keyword", "content": "Content", "issue": "Audit issue"}.get(key, key.title()), "count": type_counts.get(key, 0)}
            for key in ("issue", "content", "keyword") if type_counts.get(key, 0)
        ],
    }


def activity_status_bucket(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"completed", "resolved"}:
        return "completed"
    if normalized == "ignored":
        return "ignored"
    if normalized == "in_progress":
        return "in_progress"
    return "todo"


def activity_plan_return_target(value: str, fallback: str) -> str:
    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or parsed.path != "/modules/activity-plans":
        return fallback
    return value


def content_inventory_pages(crawl_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pages = []
    for page in crawl_pages:
        status_code = safe_int(page.get("status_code"))
        content_type = str(page.get("content_type") or "").lower()
        url = str(page.get("url") or "").strip()
        if not url or (status_code and status_code >= 400):
            continue
        if content_type and "html" not in content_type:
            continue
        pages.append(page)
    return pages


def build_content_optimization_summary(crawl_pages: list[dict[str, Any]]) -> dict[str, Any]:
    pages = content_inventory_pages(crawl_pages)
    rows: list[dict[str, Any]] = []
    failed_checks = 0
    thin_pages = 0
    missing_metadata_pages = 0
    for page in pages:
        title = str(page.get("title") or "").strip()
        meta = str(page.get("meta_description") or "").strip()
        word_count = safe_int(page.get("word_count"))
        h1_count = safe_int(page.get("h1_count"))
        issues: list[dict[str, str]] = []
        if not title:
            issues.append({"label": "Missing title", "action": "Add a unique, descriptive title of roughly 30-60 characters."})
        elif len(title) < 20:
            issues.append({"label": "Title is too short", "action": "Clarify the page topic and value without repeating keywords."})
        elif len(title) > 65:
            issues.append({"label": "Title may truncate", "action": "Shorten the title while keeping the primary topic near the beginning."})
        if not meta:
            issues.append({"label": "Missing meta description", "action": "Write a unique summary that explains the page benefit and expected content."})
        elif len(meta) < 70:
            issues.append({"label": "Meta description is too short", "action": "Add useful context and a clear reason to visit the page."})
        elif len(meta) > 170:
            issues.append({"label": "Meta description may truncate", "action": "Keep the strongest benefit and remove repeated wording."})
        if h1_count == 0:
            issues.append({"label": "Missing H1", "action": "Add one visible H1 that describes the page's main purpose."})
        elif h1_count > 1:
            issues.append({"label": "Multiple H1 headings", "action": "Keep one primary H1 and use H2/H3 for supporting sections."})
        if 0 < word_count < 150:
            thin_pages += 1
            issues.append({"label": "Thin content", "action": "Add original copy that answers user questions and supports the page purpose."})
        if not title or not meta:
            missing_metadata_pages += 1
        failed_checks += len(issues)
        if issues:
            points = round(min(15.0, len(issues) * 2.5 + (2.5 if not title else 0)), 1)
            rows.append(
                {
                    "url": str(page.get("url") or ""),
                    "title": title or "Untitled page",
                    "word_count": word_count,
                    "h1_count": h1_count,
                    "meta": meta,
                    "issues": issues,
                    "issue_count": len(issues),
                    "priority": "High" if len(issues) >= 3 or not title else "Medium" if len(issues) == 2 else "Quick win",
                    "points": points,
                    "action_title": f"Improve content quality for {title or str(page.get('url') or 'page')}",
                    "action_note": " ".join(f"{issue['label']}: {issue['action']}" for issue in issues),
                }
            )
    rows.sort(key=lambda row: (-row["issue_count"], row["url"]))
    total_checks = len(pages) * 4
    score = round(max(0, 100 - (failed_checks / total_checks * 100)), 1) if total_checks else 0.0
    return {
        "status": "Ready" if pages else "Run crawl",
        "score": score,
        "pages": len(pages),
        "healthy_pages": max(0, len(pages) - len(rows)),
        "needs_attention": len(rows),
        "thin_pages": thin_pages,
        "missing_metadata_pages": missing_metadata_pages,
        "rows": rows[:100],
    }


def normalized_duplicate_value(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def simhash_distance(left: str, right: str) -> int:
    try:
        return (int(str(left), 16) ^ int(str(right), 16)).bit_count()
    except (TypeError, ValueError):
        return 64


def duplicate_primary_page(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer the most substantial, shallowest URL as the consolidation target."""
    return sorted(
        pages,
        key=lambda page: (
            -safe_int(page.get("word_count")),
            urlparse(str(page.get("url") or "")).path.count("/"),
            len(str(page.get("url") or "")),
            str(page.get("url") or ""),
        ),
    )[0]


def enrich_duplicate_group(group: dict[str, Any], source_pages: list[dict[str, Any]]) -> dict[str, Any]:
    page_lookup = {str(page.get("url") or ""): page for page in source_pages}
    matched_pages = [page_lookup[url] for url in group.get("pages", []) if url in page_lookup]
    if not matched_pages:
        return group
    primary = duplicate_primary_page(matched_pages)
    primary_url = str(primary.get("url") or "")
    duplicate_pages = []
    for page in matched_pages:
        url = str(page.get("url") or "")
        if url == primary_url:
            continue
        duplicate_pages.append(
            {
                "url": url,
                "title": str(page.get("title") or "Untitled page"),
                "word_count": safe_int(page.get("word_count")),
                "recommendation": (
                    "Merge and redirect if this page serves the same visitor intent; otherwise rewrite its unique topic and metadata."
                    if group.get("field") in {"body_exact", "body_near"}
                    else "Rewrite this field so it describes this page's specific purpose and search intent."
                ),
            }
        )
    return {
        **group,
        "primary_page": {
            "url": primary_url,
            "title": str(primary.get("title") or "Untitled page"),
            "word_count": safe_int(primary.get("word_count")),
        },
        "duplicate_pages": duplicate_pages,
        "points": round(min(20.0, 2.5 + max(0, len(matched_pages) - 1) * 2.5), 1),
        "decision_note": "The suggested primary page has the most measured content; review traffic, backlinks, and conversions before redirecting anything.",
    }


def build_ecommerce_summary(
    crawl_pages: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> dict[str, Any]:
    pages = content_inventory_pages(crawl_pages)
    classified = []
    type_counts: Counter[str] = Counter()
    for page in pages:
        page_type = classify_ecommerce_page(page)
        type_counts[page_type] += 1
        classified.append({**page, "ecommerce_type": page_type})

    commerce_types = {"Product", "Collection", "Subscription", "Promotion", "Conversion path"}
    commerce_pages = [page for page in classified if page["ecommerce_type"] in commerce_types]
    opportunities = build_ecommerce_opportunities(commerce_pages or classified[:20], issues)
    blocking = [item for item in opportunities if item["priority"] in {"High", "Medium"}]
    score = max(0, min(100, 88 - len(blocking) * 6 - max(0, len(opportunities) - len(blocking)) * 2))
    if not pages:
        score = 0

    return {
        "status": "Ready" if pages else "Run crawl",
        "score": str(score),
        "total_pages": str(len(pages)),
        "commerce_pages": str(len(commerce_pages)),
        "product_pages": str(type_counts.get("Product", 0)),
        "collection_pages": str(type_counts.get("Collection", 0)),
        "subscription_pages": str(type_counts.get("Subscription", 0)),
        "promotion_pages": str(type_counts.get("Promotion", 0)),
        "conversion_pages": str(type_counts.get("Conversion path", 0)),
        "opportunities": opportunities[:8],
        "page_types": [
            {"label": label, "count": str(type_counts.get(label, 0)), "detail": detail}
            for label, detail in [
                ("Product", "Product detail pages and Shopify product handles"),
                ("Collection", "Collection, category and merchandising pages"),
                ("Subscription", "Recurring order, coffee club or subscribe pages"),
                ("Promotion", "Sale, bundle, offer and campaign landing pages"),
                ("Conversion path", "Cart, checkout, account, quote and lead paths"),
                ("Content assist", "Blogs, guides and advice pages that support product discovery"),
            ]
        ],
        "operations": build_ecommerce_operations(type_counts, opportunities),
        "tracking_events": build_conversion_tracking_events(type_counts),
        "subscription_checks": build_subscription_checks(type_counts),
    }


def classify_ecommerce_page(page: dict[str, Any]) -> str:
    url = str(page.get("url") or "").lower()
    title = str(page.get("title") or "").lower()
    path = urlparse(url).path.lower()
    text = f"{path} {title}"
    if any(token in text for token in ["checkout", "cart", "thank-you", "order", "account", "register", "login", "quote", "contact"]):
        return "Conversion path"
    if any(token in text for token in ["subscription", "subscribe", "coffee-club", "recurring", "recharge"]):
        return "Subscription"
    if any(token in path for token in ["/products/", "/product/", "/p/"]):
        return "Product"
    if any(token in path for token in ["/collections/", "/collection/", "/categories/", "/category/", "/shop/"]):
        return "Collection"
    if any(token in text for token in ["sale", "promotion", "promo", "offer", "discount", "bundle", "clearance", "campaign"]):
        return "Promotion"
    if any(token in path for token in ["/blog", "/blogs", "/guide", "/guides", "/learn", "/news"]):
        return "Content assist"
    return "Other"


def build_ecommerce_opportunities(
    pages: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for page in pages:
        url = str(page.get("url") or "")
        if not url:
            continue
        page_type = str(page.get("ecommerce_type") or classify_ecommerce_page(page))
        title = str(page.get("title") or "Untitled page").strip() or "Untitled page"
        meta = str(page.get("meta_description") or "").strip()
        word_count = safe_int(page.get("word_count"))
        h1_count = safe_int(page.get("h1_count"))
        status_code = safe_int(page.get("status_code"))
        technical = page.get("technical_data") if isinstance(page.get("technical_data"), dict) else {}
        schema_types = {str(value).lower() for value in technical.get("structured_data_types", [])}
        open_graph = technical.get("open_graph") if isinstance(technical.get("open_graph"), dict) else {}
        page_issues = [issue for issue in issues if issue_matches_page(url, normalize_issue_detail(issue) or issue)]
        findings: list[str] = []
        if status_code >= 400:
            findings.append(f"Page returns HTTP {status_code}")
        if not meta:
            findings.append("Missing meta description")
        elif len(meta) < 70:
            findings.append("Meta description is too short")
        if h1_count == 0:
            findings.append("Missing H1")
        elif h1_count > 1:
            findings.append("Multiple H1 headings")
        if page_type in {"Product", "Collection"} and 0 < word_count < 150:
            findings.append("Thin commercial copy")
        if page_type == "Product" and "product" not in schema_types:
            findings.append("Product schema not detected")
        if page_type in {"Product", "Collection", "Promotion"} and not (open_graph.get("title") and open_graph.get("description")):
            findings.append("Social preview metadata incomplete")
        if page_issues:
            findings.append(f"{len(page_issues)} audit finding(s) attached")
        if not findings:
            continue

        priority = "High" if status_code >= 400 or len(findings) >= 4 else "Medium" if len(findings) >= 2 else "Quick win"
        rows.append(
            {
                "page": url,
                "title": title,
                "type": page_type,
                "priority": priority,
                "owner": ecommerce_owner(page_type, findings),
                "finding": "; ".join(findings[:3]),
                "action": ecommerce_action(page_type, findings),
                "metric": ecommerce_metric(page_type),
            }
        )
    priority_order = {"High": 0, "Medium": 1, "Quick win": 2}
    return sorted(rows, key=lambda row: (priority_order.get(row["priority"], 9), row["type"], row["page"]))


def ecommerce_owner(page_type: str, findings: list[str]) -> str:
    joined = " ".join(findings).lower()
    if "http" in joined or "schema" in joined or "social preview" in joined:
        return "Development / Shopify theme"
    if page_type in {"Product", "Collection"}:
        return "Merchandising / eCommerce"
    if page_type in {"Promotion", "Subscription"}:
        return "Marketing"
    return "eCommerce"


def ecommerce_action(page_type: str, findings: list[str]) -> str:
    joined = " ".join(findings).lower()
    if "http" in joined:
        return "Restore the page, update navigation/product links, or create a relevant 301 redirect."
    if "schema" in joined:
        return "Check the Shopify product template and make sure Product JSON-LD is emitted correctly."
    if page_type == "Product":
        return "Improve product title, benefits, specifications, image alt text, schema, and internal links."
    if page_type == "Collection":
        return "Add collection intro copy, filters/merchandising context, internal links, and unique metadata."
    if page_type == "Subscription":
        return "Clarify subscribe-and-save value, frequency, cancellation, retention offer, and tracking events."
    if page_type == "Promotion":
        return "Check campaign landing copy, offer clarity, UTM consistency, expiry messaging, and conversion CTA."
    return "Review the page in Shopify/admin workflow and fix the highest-impact metadata or UX issue."


def ecommerce_metric(page_type: str) -> str:
    return {
        "Product": "Product views, add_to_cart rate, conversion rate",
        "Collection": "Collection CTR, product clicks, add_to_cart rate",
        "Subscription": "Subscription starts, retention, lifetime value",
        "Promotion": "Campaign conversion rate, revenue, bounce rate",
        "Conversion path": "Checkout progression and form completion",
    }.get(page_type, "Engagement and assisted conversion")


def build_ecommerce_operations(
    type_counts: Counter[str], opportunities: list[dict[str, str]]
) -> list[dict[str, str]]:
    high_count = sum(1 for item in opportunities if item["priority"] == "High")
    return [
        {
            "name": "Product management",
            "status": "Ready" if type_counts.get("Product") else "Needs product crawl",
            "detail": f"{type_counts.get('Product', 0)} product page(s) detected; review metadata, copy, schema and images.",
        },
        {
            "name": "Collections and merchandising",
            "status": "Ready" if type_counts.get("Collection") else "Needs collection URLs",
            "detail": f"{type_counts.get('Collection', 0)} collection page(s) detected; check filters, copy, internal links and seasonal ranges.",
        },
        {
            "name": "Subscriptions",
            "status": "Review" if type_counts.get("Subscription") else "Plan needed",
            "detail": "Use subscription landing pages and product options to explain value, frequency, cancellation and retention messaging.",
        },
        {
            "name": "Campaign and promotions",
            "status": "Review" if type_counts.get("Promotion") else "Plan needed",
            "detail": "Use promotion pages with UTM tracking, offer clarity, expiry messaging and conversion checks.",
        },
        {
            "name": "CRO action queue",
            "status": "Needs fixes" if high_count else "Ready",
            "detail": f"{len(opportunities)} eCommerce opportunity item(s), including {high_count} high-priority blocker(s).",
        },
    ]


def build_conversion_tracking_events(type_counts: Counter[str]) -> list[dict[str, str]]:
    return [
        {"event": "view_item", "where": "Product pages", "status": "Validate in GTM" if type_counts.get("Product") else "Needs product pages", "why": "Measures product detail engagement before add to cart."},
        {"event": "add_to_cart", "where": "Product cards and PDP CTA", "status": "Validate in GTM", "why": "Core ecommerce intent signal for CRO and Google Ads."},
        {"event": "begin_checkout", "where": "Cart or checkout entry", "status": "Validate in GTM" if type_counts.get("Conversion path") else "Map checkout path", "why": "Shows whether cart users progress into checkout."},
        {"event": "purchase", "where": "Order confirmation page", "status": "Protect before publishing", "why": "Primary revenue conversion used by GA4 and Google Ads."},
        {"event": "generate_lead", "where": "Contact, quote or phone CTA", "status": "Validate in GTM" if type_counts.get("Conversion path") else "Optional", "why": "Useful for quote forms, phone calls and B2B ecommerce paths."},
        {"event": "sign_up", "where": "Newsletter and account registration", "status": "Validate in GTM", "why": "Measures list growth, account creation and lifecycle marketing entry."},
        {"event": "subscribe", "where": "Subscription program CTA", "status": "Plan event" if not type_counts.get("Subscription") else "Validate in GTM", "why": "Connects subscription acquisition to retention and lifetime value."},
    ]


def build_subscription_checks(type_counts: Counter[str]) -> list[dict[str, str]]:
    has_subscription = bool(type_counts.get("Subscription"))
    return [
        {"check": "Subscription value proposition", "status": "Review" if has_subscription else "Not detected", "detail": "Explain savings, frequency, flexibility, cancellation and delivery promise."},
        {"check": "Lifecycle tracking", "status": "Plan", "detail": "Track subscription start, skip, pause, cancel and repeat purchase events."},
        {"check": "Retention content", "status": "Plan", "detail": "Add FAQ, brewing/use guidance, replenishment reminders and win-back messaging."},
        {"check": "App governance", "status": "Manual review", "detail": "Check Shopify subscription app settings, checkout compatibility and customer-service handoff."},
    ]


def build_wordpress_summary(
    crawl_pages: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> dict[str, Any]:
    pages = content_inventory_pages(crawl_pages)
    classified = []
    type_counts: Counter[str] = Counter()
    for page in pages:
        page_type = classify_wordpress_page(page)
        type_counts[page_type] += 1
        classified.append({**page, "wordpress_type": page_type})

    action_queue = build_wordpress_action_queue(classified, issues)
    blocker_count = sum(1 for item in action_queue if item["priority"] == "High")
    warning_count = sum(1 for item in action_queue if item["priority"] == "Medium")
    score = max(0, min(100, 90 - blocker_count * 9 - warning_count * 4 - max(0, len(action_queue) - blocker_count - warning_count) * 1))
    if not pages:
        score = 0

    return {
        "status": "Ready" if pages else "Run crawl",
        "score": str(score),
        "total_pages": str(len(pages)),
        "blockers": str(blocker_count),
        "warnings": str(warning_count),
        "content_pages": str(type_counts.get("Content page", 0)),
        "blog_pages": str(type_counts.get("Blog / news", 0)),
        "service_pages": str(type_counts.get("Service page", 0)),
        "commerce_pages": str(type_counts.get("WooCommerce", 0)),
        "forms_pages": str(type_counts.get("Forms / leads", 0)),
        "page_types": [
            {"label": label, "count": str(type_counts.get(label, 0)), "detail": detail}
            for label, detail in [
                ("Homepage", "Static homepage and top-level brand entry point"),
                ("Content page", "Standard WordPress pages such as About, Services and FAQs"),
                ("Service page", "Lead-generation service pages that need clear H1, CTA and metadata"),
                ("Blog / news", "Posts, articles and content marketing pages"),
                ("WooCommerce", "Product, shop, cart and checkout pages"),
                ("Forms / leads", "Contact, enquiry, quote and booking paths"),
            ]
        ],
        "checks": build_wordpress_checks(pages, action_queue),
        "action_queue": action_queue[:10],
        "launch_steps": build_wordpress_launch_steps(type_counts),
        "handover": build_wordpress_handover_items(type_counts),
    }


def classify_wordpress_page(page: dict[str, Any]) -> str:
    url = str(page.get("url") or "").lower()
    title = str(page.get("title") or "").lower()
    path = urlparse(url).path.lower().strip("/")
    text = f"{path} {title}"
    if not path:
        return "Homepage"
    if any(token in text for token in ["product", "shop", "cart", "checkout", "my-account", "woocommerce"]):
        return "WooCommerce"
    if any(token in text for token in ["contact", "enquiry", "quote", "booking", "form", "appointment"]):
        return "Forms / leads"
    if any(token in path for token in ["blog", "news", "article", "post", "insight"]):
        return "Blog / news"
    if any(token in text for token in ["service", "services", "solutions", "repair", "installation", "consulting"]):
        return "Service page"
    if any(token in text for token in ["privacy", "terms", "cookie", "policy"]):
        return "Policy page"
    return "Content page"


def build_wordpress_action_queue(
    pages: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for page in pages:
        url = str(page.get("url") or "")
        if not url:
            continue
        page_type = str(page.get("wordpress_type") or classify_wordpress_page(page))
        title = str(page.get("title") or "Untitled page").strip() or "Untitled page"
        meta = str(page.get("meta_description") or "").strip()
        word_count = safe_int(page.get("word_count"))
        h1_count = safe_int(page.get("h1_count"))
        status_code = safe_int(page.get("status_code"))
        technical = page.get("technical_data") if isinstance(page.get("technical_data"), dict) else {}
        headers = technical.get("security_headers") if isinstance(technical.get("security_headers"), dict) else {}
        page_issues = [issue for issue in issues if issue_matches_page(url, normalize_issue_detail(issue) or issue)]
        findings: list[str] = []
        if status_code >= 400:
            findings.append(f"HTTP {status_code} page error")
        if not meta:
            findings.append("Missing meta description")
        elif len(meta) < 70:
            findings.append("Short meta description")
        if h1_count == 0:
            findings.append("Missing H1")
        elif h1_count > 1:
            findings.append("Multiple H1 headings")
        if page_type in {"Homepage", "Service page", "WooCommerce"} and 0 < word_count < 180:
            findings.append("Thin page copy")
        if technical and not technical.get("indexable", True):
            findings.append("Noindex detected")
        if technical and not technical.get("canonical"):
            findings.append("Missing canonical")
        if headers and not headers.get("strict_transport_security"):
            findings.append("Missing HSTS header")
        if page_issues:
            findings.append(f"{len(page_issues)} audit issue(s)")
        if not findings:
            continue
        priority = "High" if status_code >= 400 or "Noindex detected" in findings or len(findings) >= 4 else "Medium" if len(findings) >= 2 else "Quick win"
        rows.append(
            {
                "page": url,
                "title": title,
                "type": page_type,
                "priority": priority,
                "owner": wordpress_owner(page_type, findings),
                "finding": "; ".join(findings[:4]),
                "action": wordpress_action(page_type, findings),
                "where": wordpress_where_to_fix(page_type, findings),
            }
        )
    priority_order = {"High": 0, "Medium": 1, "Quick win": 2}
    return sorted(rows, key=lambda row: (priority_order.get(row["priority"], 9), row["type"], row["page"]))


def wordpress_owner(page_type: str, findings: list[str]) -> str:
    joined = " ".join(findings).lower()
    if "http" in joined or "hsts" in joined or "canonical" in joined:
        return "Developer / hosting"
    if page_type in {"Forms / leads", "WooCommerce"}:
        return "Website admin / plugins"
    if "meta" in joined or "copy" in joined or "h1" in joined:
        return "Content / SEO"
    return "Website owner"


def wordpress_action(page_type: str, findings: list[str]) -> str:
    joined = " ".join(findings).lower()
    if "http" in joined:
        return "Restore the page, update menu/internal links, or add a 301 redirect before launch."
    if "noindex" in joined:
        return "Check Settings > Reading and SEO plugin robots settings before publishing."
    if page_type == "Forms / leads":
        return "Test the form, recipient email, Reply-To, SMTP delivery and thank-you tracking."
    if page_type == "WooCommerce":
        return "Check product/shop metadata, schema, checkout path, payment/shipping and conversion tracking."
    if "hsts" in joined:
        return "Confirm SSL is active, force HTTPS and configure security headers at hosting/CDN level."
    return "Update WordPress page content, SEO title/meta, H1, internal links and image alt text."


def wordpress_where_to_fix(page_type: str, findings: list[str]) -> str:
    joined = " ".join(findings).lower()
    if "http" in joined or "hsts" in joined:
        return "cPanel, hosting, redirect plugin or CDN"
    if "noindex" in joined or "canonical" in joined or "meta" in joined:
        return "WordPress page editor and SEO plugin"
    if page_type == "Forms / leads":
        return "Contact Form 7 / WPForms and SMTP plugin"
    if page_type == "WooCommerce":
        return "WooCommerce product/shop settings and theme template"
    return "WordPress page editor, theme builder or media library"


def build_wordpress_checks(
    pages: list[dict[str, Any]], action_queue: list[dict[str, str]]
) -> list[dict[str, str]]:
    action_text = " ".join(item["finding"].lower() for item in action_queue)
    return [
        {"name": "Permalinks and page structure", "status": "Review" if pages else "Needs crawl", "detail": "Use clean URLs, stable slugs, sensible menus and one static homepage."},
        {"name": "SEO plugin fields", "status": "Needs fixes" if "meta" in action_text or "h1" in action_text else "Ready", "detail": "Check SEO title, meta description, H1, canonical and indexability for every important page."},
        {"name": "Forms and SMTP", "status": "Manual test", "detail": "Submit contact/quote forms, check recipient, Reply-To, SMTP and spam folder before handover."},
        {"name": "SSL and redirects", "status": "Needs fixes" if "http" in action_text or "hsts" in action_text else "Review", "detail": "Confirm HTTPS, www/non-www, mixed content, redirects and security headers."},
        {"name": "Plugins and theme risk", "status": "Manual review", "detail": "Review plugin count, updates, PHP compatibility, theme changes and backup before live edits."},
        {"name": "Backup and handover", "status": "Plan", "detail": "Take a backup, document admin users, revoke temporary credentials and provide client handover notes."},
    ]


def build_wordpress_launch_steps(type_counts: Counter[str]) -> list[dict[str, str]]:
    return [
        {"step": "Access and backup", "detail": "Confirm admin/cPanel access, live-site risk, backup status and rollback path."},
        {"step": "Core settings", "detail": "Check General, Reading, Permalinks, timezone, site title and search visibility."},
        {"step": "Content QA", "detail": "Review pages, menus, H1s, metadata, image alt text and mobile layout."},
        {"step": "Forms and email", "detail": "Test contact forms, SMTP, Reply-To and real inbox delivery."},
        {"step": "SEO and indexing", "detail": "Check sitemap, robots, canonical, noindex, redirects and Search Console submission."},
        {"step": "Performance and security", "detail": "Review caching, image size, SSL, mixed content, plugin updates and security headers."},
        {"step": "WooCommerce" if type_counts.get("WooCommerce") else "Conversion path", "detail": "Test cart/checkout/payment or lead form tracking if the site has ecommerce or enquiry journeys."},
        {"step": "Handover", "detail": "Document what changed, what was tested, open risks and recommended follow-up work."},
    ]


def build_wordpress_handover_items(type_counts: Counter[str]) -> list[dict[str, str]]:
    return [
        {"label": "What was changed", "example": "Updated page content, metadata, form settings, redirects or plugin/theme configuration."},
        {"label": "What was tested", "example": "Homepage, key pages, forms, mobile layout, SSL, sitemap, SEO fields and checkout/lead journeys."},
        {"label": "Risks or pending confirmations", "example": "DNS propagation, plugin conflicts, email deliverability, payment gateway or client content approval."},
        {"label": "Client should check next", "example": "Submit a form, place a test order if WooCommerce is present, and confirm business details are correct."},
        {"label": "Recommended follow-up", "example": "Run OpenAudit scans monthly and after plugin/theme/content changes."},
    ]


def near_duplicate_body_groups(
    pages: list[dict[str, Any]],
    excluded_hashes: set[str],
    threshold: int = 5,
) -> list[dict[str, Any]]:
    candidates = [
        page for page in pages
        if page.get("content_simhash") and str(page.get("content_hash") or "") not in excluded_hashes
    ]
    parent = list(range(len(candidates)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left_index: int, right_index: int) -> None:
        left_root = find(left_index)
        right_root = find(right_index)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index, left_page in enumerate(candidates):
        left_words = max(1, safe_int(left_page.get("word_count")))
        for right_index in range(left_index + 1, len(candidates)):
            right_page = candidates[right_index]
            right_words = max(1, safe_int(right_page.get("word_count")))
            if min(left_words, right_words) / max(left_words, right_words) < 0.7:
                continue
            if simhash_distance(
                str(left_page.get("content_simhash") or ""),
                str(right_page.get("content_simhash") or ""),
            ) <= threshold:
                union(left_index, right_index)

    components: dict[int, list[dict[str, Any]]] = {}
    for index, page in enumerate(candidates):
        components.setdefault(find(index), []).append(page)
    groups = []
    for component in components.values():
        if len(component) < 2:
            continue
        distances = [
            simhash_distance(str(left.get("content_simhash")), str(right.get("content_simhash")))
            for index, left in enumerate(component)
            for right in component[index + 1:]
        ]
        closest_distance = min(distances) if distances else threshold
        groups.append(
            enrich_duplicate_group({
                "field": "body_near",
                "label": "Near-duplicate body",
                "value": f"Very similar body wording (SimHash distance {threshold}/64 or less)",
                "pages": [str(page.get("url") or "") for page in component],
                "count": len(component),
                "risk": "High" if len(component) >= 4 else "Medium",
                "action": "Choose the primary page, consolidate overlapping copy, and rewrite the remaining pages around their unique purpose.",
                "similarity": f"About {round((64 - closest_distance) / 64 * 100)}% fingerprint similarity",
            }, pages)
        )
    return groups


def build_duplicate_content_summary(crawl_pages: list[dict[str, Any]]) -> dict[str, Any]:
    pages = content_inventory_pages(crawl_pages)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for page in pages:
        for field, label, minimum in (
            ("title", "Title", 5),
            ("meta_description", "Meta description", 30),
        ):
            original = str(page.get(field) or "").strip()
            normalized = normalized_duplicate_value(original)
            if len(normalized) < minimum:
                continue
            group = grouped.setdefault(
                (field, normalized),
                {"field": field, "label": label, "value": original, "pages": []},
            )
            group["pages"].append(str(page.get("url") or ""))

        content_hash = str(page.get("content_hash") or "").strip().lower()
        if len(content_hash) == 64:
            body_group = grouped.setdefault(
                ("body_exact", content_hash),
                {
                    "field": "body_exact",
                    "label": "Exact body content",
                    "value": "Identical privacy-preserving body fingerprint",
                    "pages": [],
                },
            )
            body_group["pages"].append(str(page.get("url") or ""))

    groups = []
    affected_pages: set[str] = set()
    exact_body_hashes: set[str] = set()
    for group_key, group in grouped.items():
        unique_pages = list(dict.fromkeys(group["pages"]))
        if len(unique_pages) < 2:
            continue
        if group["field"] == "body_exact":
            exact_body_hashes.add(group_key[1])
        affected_pages.update(unique_pages)
        if group["field"] == "title":
            action = "Give each page a title that reflects its unique product, service, or intent."
        elif group["field"] == "meta_description":
            action = "Write a distinct description that summarizes the specific page instead of reusing a site-wide default."
        else:
            action = "Choose the primary page, consolidate identical copy where appropriate, and rewrite pages that serve a different intent."
        groups.append(
            enrich_duplicate_group({
                **group,
                "pages": unique_pages,
                "count": len(unique_pages),
                "risk": "High" if len(unique_pages) >= 4 else "Medium",
                "action": action,
                "similarity": "100% exact match",
            }, pages)
        )
    near_body_groups = near_duplicate_body_groups(pages, exact_body_hashes)
    groups.extend(near_body_groups)
    for group in near_body_groups:
        affected_pages.update(group["pages"])
    groups.sort(key=lambda group: (-group["count"], group["label"], group["value"].lower()))
    duplicate_title_groups = sum(1 for group in groups if group["field"] == "title")
    duplicate_meta_groups = sum(1 for group in groups if group["field"] == "meta_description")
    exact_body_groups = sum(1 for group in groups if group["field"] == "body_exact")
    near_body_group_count = sum(1 for group in groups if group["field"] == "body_near")
    fingerprinted_pages = sum(1 for page in pages if page.get("content_simhash"))
    affected_ratio = len(affected_pages) / len(pages) if pages else 0
    score = round(max(0, 100 - affected_ratio * 70), 1) if pages else 0.0
    return {
        "status": "Ready" if pages else "Run crawl",
        "score": score,
        "pages": len(pages),
        "affected_pages": len(affected_pages),
        "duplicate_title_groups": duplicate_title_groups,
        "duplicate_meta_groups": duplicate_meta_groups,
        "exact_body_groups": exact_body_groups,
        "near_body_groups": near_body_group_count,
        "fingerprinted_pages": fingerprinted_pages,
        "groups": groups[:50],
        "body_similarity_status": "Live" if fingerprinted_pages else "Run a new Content scan",
    }


def build_issue_context_summary(
    issue: dict[str, Any],
    lighthouse_summary: dict[str, Any],
    site_url: str | None,
) -> dict[str, Any]:
    examples = issue.get("affected_examples") or []
    affected_pages = []
    seen_pages: set[str] = set()
    for example in examples:
        page_url = str(example.get("page_url") or "").strip()
        if page_url and page_url not in seen_pages:
            affected_pages.append(page_url)
            seen_pages.add(page_url)

    report_site = str(
        lighthouse_summary.get("report_source", {}).get("url")
        or lighthouse_summary.get("final_url")
        or ""
    )
    selected_site = site_url or report_site
    site_match = True
    if site_url and report_site:
        site_match = site_key(site_url) == site_key(report_site)

    first_step = ""
    steps = issue.get("fix_guidance", {}).get("steps") or []
    if steps:
        first_step = str(steps[0])

    return {
        "selected_site": selected_site,
        "report_site": report_site,
        "site_match": site_match,
        "affected_pages": affected_pages[:5],
        "affected_page_count": len(affected_pages) or int(issue.get("pages") or 0),
        "occurrence_count": int(issue.get("occurrences") or 0),
        "owner": str(issue.get("fix_guidance", {}).get("owner") or issue.get("responsibility") or "Team"),
        "first_step": first_step,
        "success_signal": str(issue.get("fix_guidance", {}).get("success_signal") or ""),
    }


def normalize_issue_detail(issue: dict[str, Any] | None) -> dict[str, Any] | None:
    if not issue:
        return None

    normalized = dict(issue)
    examples = normalized.get("affected_examples") or normalized.get("evidence") or []
    occurrences = int(normalized.get("occurrences") or len(examples) or 1)
    page_urls = {
        str(example.get("page_url") or "").strip()
        for example in examples
        if str(example.get("page_url") or "").strip()
    }
    page_count = int(normalized.get("pages") or len(page_urls) or 1)
    points_value = float(normalized.get("points") or 0)
    if points_value <= 0:
        points_value = float(page_count or 1)

    normalized.update(
        {
            "category": str(normalized.get("category") or "Issue"),
            "recommendation": str(
                normalized.get("recommendation")
                or normalized.get("summary")
                or normalized.get("description")
                or "Review the affected examples and apply the recommended fix."
            ),
            "conformance": str(normalized.get("conformance") or normalized.get("category") or "Review"),
            "difficulty": str(normalized.get("difficulty") or "Medium"),
            "responsibility": str(
                normalized.get("responsibility") or normalized.get("owner") or "Development"
            ),
            "element": str(normalized.get("element") or "Component"),
            "occurrences": str(occurrences),
            "pages": str(page_count),
            "points": f"{points_value:.1f}",
            "affected_examples": examples,
        }
    )
    normalized["fix_guidance"] = normalized.get("fix_guidance") or build_managed_issue_fix_guidance(
        normalized
    )
    return normalized


def build_managed_issue_fix_guidance(issue: dict[str, Any]) -> dict[str, Any]:
    source = str(issue.get("source") or "").lower()
    owner = str(issue.get("owner") or issue.get("responsibility") or "Development")
    priority = str(issue.get("priority") or issue.get("difficulty") or "Medium")
    examples = issue.get("affected_examples") or []
    first_page = ""
    if examples:
        first_page = str(examples[0].get("page_url") or "").strip()

    if source == "budget":
        audit_id = str(issue.get("audit_id") or issue.get("issue_key") or "")
        measured = str((examples[0] if examples else {}).get("explanation") or "Review the current value against the configured threshold.")
        if "largest-contentful-paint" in audit_id:
            change = "Optimize the LCP element, server response, critical image or font loading, and render-blocking resources."
            checks = ["LCP is at or below the configured millisecond limit.", "The page remains visually stable and the primary content still renders correctly."]
        elif "cumulative-layout-shift" in audit_id:
            change = "Reserve dimensions for media and embeds, stabilize fonts, and prevent late banners or widgets from moving content."
            checks = ["CLS is at or below the configured limit.", "No visible content jumps during page load or interaction."]
        else:
            change = "Fix the highest-impact failed audits in this category until the score reaches the configured minimum."
            checks = ["The category score reaches the configured minimum.", "No new critical regression is introduced by the changes."]
        return {
            "owner": owner, "priority": priority,
            "handoff_note": measured,
            "success_signal": checks[0],
            "what_to_change": [change],
            "why_it_matters": "This page is outside the quality threshold agreed for the selected website.",
            "where_to_change": [{"place": first_page or "Affected page or shared template", "detail": measured}],
            "steps": ["Open the latest Lighthouse report for the affected page.", change, "Run a new Lighthouse or Full audit."],
            "validation": checks,
            "acceptance_criteria": checks,
            "code_hint": "Adjust thresholds only when the business requirement changes; do not raise a limit merely to make the warning disappear.",
        }

    if source == "pa11y":
        guidance = build_pa11y_guidance(issue)
        return {
            "owner": guidance.get("owner", owner),
            "priority": priority,
            "handoff_note": guidance.get("summary", ""),
            "success_signal": guidance.get("verify", ""),
            "what_to_change": [guidance.get("summary", "Fix the accessibility markup or content.")],
            "why_it_matters": "This issue affects accessibility and should be fixed in the source component so it stays fixed across pages.",
            "where_to_change": [
                {
                    "place": guidance.get("where", "Component or template"),
                    "detail": "Use the evidence and selector below to locate the shared source.",
                }
            ],
            "steps": guidance.get("steps", []),
            "validation": [guidance.get("verify", "Run the same Pa11y scan again and confirm the issue is gone.")],
            "acceptance_criteria": [
                "The same Pa11y finding no longer appears for the affected page.",
                "The markup remains valid and accessible after the change.",
            ],
            "code_hint": guidance.get("good_example", ""),
        }

    location = "Affected page or shared template"
    if first_page:
        location = first_page
    return {
        "owner": owner,
        "priority": priority,
        "handoff_note": "Use the affected example and rescan the website after the fix ships.",
        "success_signal": "The issue disappears or the occurrence count drops in the next scan.",
        "what_to_change": [
            str(issue.get("recommendation") or "Apply the recommended fix to the affected page or template.")
        ],
        "why_it_matters": "Leaving the issue unresolved reduces quality score and keeps the same problem live on the website.",
        "where_to_change": [
            {
                "place": location,
                "detail": "Start with the affected example below, then fix the shared template if more than one page is involved.",
            }
        ],
        "steps": [
            "Open the affected page or template and confirm the problem using the evidence below.",
            "Apply the smallest safe fix that removes the issue at source.",
            "Run a fresh scan for this website to confirm the issue is resolved.",
        ],
        "validation": [
            "A new scan shows the issue removed or reduced.",
            "The page still renders correctly after the change.",
        ],
        "acceptance_criteria": [
            "The issue no longer appears in the next scan.",
            "The fix is applied in the shared source when multiple pages are affected.",
        ],
        "code_hint": "Use the affected selector, page URL, and rendered output to locate the source component quickly.",
    }


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


def load_lighthouse_report_summary(
    site_url: str | None = None,
    include_keywords: bool = True,
    crawl_pages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
            "link_integrity": build_link_integrity_summary(load_reports(site_url), crawl_pages or []),
            "document_governance": build_document_governance_summary([]),
            "privacy_summary": build_privacy_summary({}),
            "response_summary": build_response_summary({}),
            "connector_summary": build_connector_summary(load_reports(site_url)),
            "crawler_summary": build_crawler_summary({}, {}, load_reports(site_url), crawl_pages or []),
            "architecture_summary": build_architecture_summary({}, []),
            "comparison_summary": build_comparison_summary([]),
            "ai_recommendations": build_ai_recommendations([]),
            "report_href": "",
            "json_href": "",
            "final_url": "",
            "generated_at": "",
            "report_source": {},
        }

    cache_key = f"{lighthouse_cache_key(json_report)}:{report_collection_cache_key(site_url)}:{search_console_cache_key(site_url)}:{crawl_inventory_cache_key(crawl_pages or [])}:keywords={include_keywords}"
    if cache_key in _LIGHTHOUSE_SUMMARY_CACHE:
        return _LIGHTHOUSE_SUMMARY_CACHE[cache_key]

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
            "link_integrity": build_link_integrity_summary(load_reports(site_url), crawl_pages or []),
            "document_governance": build_document_governance_summary([]),
            "privacy_summary": build_privacy_summary({}),
            "response_summary": build_response_summary({}),
            "connector_summary": build_connector_summary(load_reports(site_url)),
            "crawler_summary": build_crawler_summary({}, {}, load_reports(site_url), crawl_pages or []),
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
            has_visual_evidence = any(
                evidence.get("screenshot_path") for evidence in lifecycle.get("evidence", [])
            )
            if lifecycle.get("evidence") and (not issue.get("affected_examples") or has_visual_evidence):
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
        "keyword_suggestions": (
            build_keyword_suggestions(data, audits, issues_to_fix, reports)
            if include_keywords else build_keyword_suggestions({}, {}, [], [])
        ),
        "accessibility_breakdown": build_accessibility_breakdown(categories, issues_to_fix),
        "prepublish_summary": build_prepublish_summary(issues_to_fix),
        "analytics_summary": build_analytics_summary(audits, issues_to_fix, reports),
        "campaign_summary": build_campaign_summary(),
        "behavior_summary": build_behavior_summary(audits),
        "content_quality": build_content_quality_summary(audits),
        "link_integrity": build_link_integrity_summary(reports, crawl_pages or []),
        "document_governance": build_document_governance_summary(reports),
        "privacy_summary": build_privacy_summary(audits),
        "response_summary": build_response_summary(audits),
        "connector_summary": build_connector_summary(reports),
        "crawler_summary": build_crawler_summary(data, audits, reports, crawl_pages or []),
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
    _LIGHTHOUSE_SUMMARY_CACHE[cache_key] = summary
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


def crawl_inventory_cache_key(crawl_pages: list[dict[str, Any]]) -> str:
    if not crawl_pages:
        return "crawl-pages=0"
    urls = [str(page.get("url") or "") for page in crawl_pages if page.get("url")]
    return f"crawl-pages={len(crawl_pages)}:{hash('|'.join(sorted(urls[:50])))}"


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
        issue_identity_values = {
            str(issue.get("id") or ""),
            str(issue.get("audit_id") or ""),
            str(issue.get("issue_key") or ""),
        }
        issue_key_value = str(issue.get("issue_key") or "")
        if ":" in issue_key_value:
            issue_identity_values.add(issue_key_value.split(":", 1)[1])
        if issue_id in issue_identity_values:
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
            {"name": "Activity Plan overview", "status": "Live"},
            {"name": "Issues and recommendations", "status": "Live"},
            {"name": "Content optimization", "status": "Live"},
            {"name": "Duplicate content", "status": "Live"},
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

KEYWORD_GENERIC_PHRASE_TOKENS = {
    "australian",
    "automotive",
    "company",
    "industrial",
    "quality",
    "series",
    "solutions",
}

KEYWORD_PRODUCT_NOUNS = {
    "bar",
    "bracket",
    "indicator",
    "kit",
    "lamp",
    "lamps",
    "light",
    "lightbar",
    "lightbars",
    "lights",
    "marker",
    "reflector",
    "strut",
    "tray",
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
    crawl_urls = load_keyword_crawl_urls(final_url)
    source_urls = build_keyword_source_urls(final_url, crawl_urls, sitemap_urls)

    page_title = audit_text(audits.get("document-title"))
    meta_description = audit_text(audits.get("meta-description"))
    text_sources = [page_title, meta_description]
    counter: Counter[str] = Counter()
    url_sources: dict[str, set[str]] = {}
    content_source_urls = source_urls[:12]
    content_keywords = extract_content_keywords(content_source_urls)
    search_console = build_search_console_summary(final_url)
    brand_name = seo_brand_name(final_url)
    page_snapshots = build_keyword_page_snapshots(source_urls[:20])
    search_console_actions = build_search_console_actions(
        search_console.get("rows", []),
        page_snapshots,
        issues,
    )

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
    seen_candidates: set[tuple[str, str]] = set()
    for raw_keyword, count in counter.most_common(60):
        keyword = raw_keyword
        urls = sorted(url_sources.get(keyword, set()))
        page = urls[0] if urls else final_url
        affected_pages = len(urls) if urls else 1
        metrics = match_search_console_metrics(keyword, page, search_console.get("rows", []))
        page_snapshot = page_snapshots.get(page, {})
        keyword = page_specific_keyword_phrase(keyword, page, page_snapshot)
        if not metrics:
            metrics = match_search_console_metrics(keyword, page, search_console.get("rows", []))
        candidate_key = (keyword.lower(), page)
        if candidate_key in seen_candidates or not keyword_candidate_is_useful(
            raw_keyword,
            keyword,
            page,
            metrics,
        ):
            continue
        seen_candidates.add(candidate_key)
        page_issues = keyword_relevant_issues(page, issues)
        score_gain = keyword_score_gain(keyword, count, affected_pages, page_issues, metrics)
        difficulty = keyword_difficulty(keyword, affected_pages, page_issues, metrics)
        opportunity = keyword_opportunity(keyword, page_issues, page_snapshot, metrics)
        focus = keyword_focus_area(page_snapshot, page_issues, page)
        why_now = keyword_why_now(keyword, page, metrics, page_snapshot, page_issues)
        decision = keyword_strategy_decision(keyword, page, page_snapshot, metrics)
        decision_label = keyword_decision_summary(keyword, page, page_snapshot, metrics, page_issues)
        confidence = keyword_decision_confidence(keyword, page, page_snapshot, metrics, page_issues)
        content_brief = (
            keyword_supporting_content_brief(keyword, page, metrics)
            if decision == "Create supporting content"
            else {}
        )
        rows.append(
            {
                "keyword": keyword,
                "intent": classify_keyword_intent(page, keyword),
                "decision": decision,
                "decision_label": decision_label,
                "confidence": confidence,
                "confidence_class": keyword_confidence_class(confidence),
                "content_brief": content_brief,
                "source_count": str(max(count, 1)),
                "affected_pages": str(affected_pages),
                "score_gain": f"{score_gain:.2f}",
                "difficulty": difficulty,
                "status": "Suggested",
                "opportunity": opportunity,
                "focus": focus,
                "page": page,
                "reason": keyword_reason(keyword, page, count, page_issues, content_keywords),
                "action": keyword_action(keyword, page, page_snapshot, metrics),
                "why_now": why_now,
                "why_it_matters": keyword_why_it_matters(keyword, page, page_snapshot, metrics, page_issues),
                "matched_query": metrics.get("query", ""),
                "current_title": page_snapshot.get("title", ""),
                "current_h1": page_snapshot.get("h1", ""),
                "current_meta": page_snapshot.get("meta", ""),
                "clicks": metrics.get("clicks", "-"),
                "impressions": metrics.get("impressions", "-"),
                "ctr": metrics.get("ctr", "-"),
                "position": metrics.get("position", "-"),
            }
        )
        if len(rows) >= 14:
            break

    rows = sorted(rows, key=keyword_priority_sort_key, reverse=True)
    page_opportunities = build_page_keyword_opportunities(source_urls[:40], issues, search_console.get("rows", []), page_snapshots)
    optimization_briefs = build_keyword_optimization_briefs(page_opportunities, rows, brand_name)
    page_edit_queue = build_page_edit_queue(optimization_briefs)
    workflow_summary = apply_keyword_action_states(
        site_key(final_url),
        page_edit_queue,
        optimization_briefs,
        page_opportunities,
        rows,
    )
    cannibalization = build_keyword_cannibalization(page_opportunities)
    status = "Generated" if rows else "Needs sitemap"
    source = keyword_source_label(sitemap_urls, crawl_urls, content_keywords)
    return {
        "status": status,
        "source": source,
        "count": len(rows),
        "rows": rows,
        "page_opportunities": page_opportunities,
        "optimization_briefs": optimization_briefs,
        "page_edit_queue": page_edit_queue,
        "workflow_summary": workflow_summary,
        "cannibalization": cannibalization,
        "search_console": search_console,
        "search_console_actions": search_console_actions,
        "tracked_pages": len(source_urls),
        "overview": build_keyword_overview(rows, source_urls, issues),
        "filters": [
            {"name": "All suggestions", "count": str(len(rows)), "active": True},
            {"name": "Highest impact", "count": str(sum(1 for row in rows if float(row["score_gain"]) >= 1.5)), "active": False},
            {"name": "Quick wins", "count": str(sum(1 for row in rows if row["difficulty"] == "Low")), "active": False},
            {"name": "Needs metadata", "count": str(sum(1 for row in rows if row.get("focus") in {"Title tag", "Meta description", "Heading structure"})), "active": False},
        ],
        "activity_plan": build_keyword_activity_plan(rows, page_opportunities),
        "content_gaps": build_keyword_content_gaps(audits, issues),
        "engine": {
            "content_extractor": "YAKE" if yake else "Not installed",
            "content_pages_sampled": str(len(content_source_urls)),
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
                "status": "Live",
                "value": "Mark recommendations as accepted, ignored, assigned or fixed",
            },
        ],
        "next_step": (
            "Connect Google Search Console later to replace these starter ideas with real queries, impressions, clicks and ranking movement."
            if not search_console.get("connected")
            else "Start with the Search Console opportunities below, because they already represent real demand from searchers."
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


def seo_brand_name(url: str) -> str:
    host = site_label(url or "").replace("www.", "")
    if not host:
        return "Your Brand"
    root = host.split(".")[0]
    words = [token for token in re.split(r"[^a-z0-9]+", root.lower()) if token]
    if not words:
        return "Your Brand"
    return " ".join(word.upper() if word in {"led", "4wd"} else word.capitalize() for word in words)


def build_keyword_page_snapshots(urls: list[str]) -> dict[str, dict[str, str]]:
    snapshots: dict[str, dict[str, str]] = {}
    unique_urls = list(dict.fromkeys(urls))[:20]
    with ThreadPoolExecutor(max_workers=min(6, len(unique_urls) or 1)) as executor:
        results = executor.map(fetch_page_seo_snapshot, unique_urls)
    for url, snapshot in zip(unique_urls, results):
        if snapshot:
            snapshots[url] = snapshot
    return snapshots


def fetch_page_seo_snapshot(url: str) -> dict[str, str]:
    html = fetch_cached_page_html(url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    h1 = ""
    h1_tag = soup.find("h1")
    if h1_tag:
        h1 = normalize_text(h1_tag.get_text(" "))
    paragraphs = [normalize_text(tag.get_text(" ")) for tag in soup.find_all("p")[:12]]
    word_count = len(" ".join(paragraphs).split())
    return {
        "title": normalize_text(soup.title.string if soup.title else ""),
        "meta": meta_content(soup, "description"),
        "h1": h1,
        "word_count": str(word_count),
    }


def extract_content_keywords(urls: list[str]) -> list[dict[str, Any]]:
    if not yake or not urls:
        return []

    extractor = yake.KeywordExtractor(lan="en", n=3, dedupLim=0.9, top=14)
    scored: dict[str, dict[str, Any]] = {}
    unique_urls = list(dict.fromkeys(urls))[:12]
    with ThreadPoolExecutor(max_workers=min(6, len(unique_urls) or 1)) as executor:
        contents = executor.map(fetch_keyword_source_text, unique_urls)
    for url, content in zip(unique_urls, contents):
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
    html = fetch_cached_page_html(url)
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
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


def fetch_cached_page_html(url: str) -> str:
    now = time.monotonic()
    cached = _PAGE_HTML_CACHE.get(url)
    if cached and now - cached[0] < (900 if cached[1] else 60):
        return cached[1]
    try:
        response = requests.get(
            url,
            timeout=3,
            headers={"User-Agent": "OpenAuditBot/1.0"},
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException:
        _PAGE_HTML_CACHE[url] = (now, "")
        return ""
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        _PAGE_HTML_CACHE[url] = (now, "")
        return ""
    html = response.text
    _PAGE_HTML_CACHE[url] = (now, html)
    if len(_PAGE_HTML_CACHE) > 300:
        oldest = sorted(_PAGE_HTML_CACHE, key=lambda key: _PAGE_HTML_CACHE[key][0])[:50]
        for key in oldest:
            _PAGE_HTML_CACHE.pop(key, None)
    return html


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


def keyword_candidate_is_useful(
    raw_keyword: str,
    target_phrase: str,
    page: str,
    metrics: dict[str, str] | None = None,
) -> bool:
    raw_tokens = keyword_tokens(raw_keyword)
    target_tokens = keyword_tokens(target_phrase)
    metrics = metrics or {}
    if len(target_tokens) < 2:
        return False
    if len(raw_tokens) == 1 and keyword_token_is_spec(raw_tokens[0]):
        return False

    host_root = site_label(page).lower().replace("www.", "").split(".")[0]
    compact_raw = "".join(raw_tokens)
    if compact_raw and host_root:
        brand_fragment = all(token in host_root for token in raw_tokens)
        if brand_fragment and len(compact_raw) >= max(5, len(host_root) // 2):
            return False

    if "company" in raw_tokens:
        return False
    if raw_tokens and set(raw_tokens).issubset(KEYWORD_GENERIC_PHRASE_TOKENS | {"led"}):
        return False

    has_product_noun = bool(set(target_tokens) & KEYWORD_PRODUCT_NOUNS)
    has_real_query = bool(str(metrics.get("query") or "").strip())
    is_informational = any(
        token in target_tokens
        for token in {"best", "choose", "compare", "guide", "how", "install", "wire"}
    )
    return has_product_noun or has_real_query or is_informational


def keyword_token_is_spec(token: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:\d+(?:\.\d+)?(?:v|w|kw|lm|mm|cm|amp|amps|pk)|\d+x\d+)",
            str(token or "").lower(),
        )
    )


def page_specific_keyword_phrase(
    keyword: str,
    page: str,
    page_snapshot: dict[str, str] | None = None,
) -> str:
    page_snapshot = page_snapshot or {}
    path = urlparse(page).path.lower()
    source = str(page_snapshot.get("h1") or path.rsplit("/", 1)[-1]).lower()
    source = re.sub(r"[^a-z0-9]+", " ", source).strip()
    source_tokens = source.split()

    if "product" in path and any(token in source_tokens for token in {"lightbar", "lightbars"}):
        size_match = re.search(r"\b(\d{1,3})\s*(?:inch|in)\b", source)
        descriptors = []
        for token in source_tokens:
            normalized = "beam" if token == "beams" else token
            if normalized in {"slimline", "combo", "beam", "curved", "dual", "single", "double", "mini"}:
                if normalized not in descriptors:
                    descriptors.append(normalized)
        parts = []
        if size_match:
            parts.append(f"{size_match.group(1)} inch")
        parts.extend(descriptors[:2])
        parts.append("LED light bar")
        return " ".join(parts)

    if "product" in path and "stop" in source_tokens and "tail" in source_tokens:
        series_match = re.search(r"\b(\d{2,4})\s*series\b", source)
        prefix = f"{series_match.group(1)} Series " if series_match else ""
        return f"{prefix}LED stop tail indicator lamp".strip()

    if "product" in path and "marker" in source_tokens and "reflector" in source_tokens:
        return "LED marker reflector light"
    if "product" in path and any(token in source_tokens for token in {"worklight", "worklights"}):
        return "LED work light"
    return recommended_keyword_phrase(keyword, page)


def keyword_source_label(
    sitemap_urls: list[str], crawl_urls: list[str], content_keywords: list[dict[str, Any]]
) -> str:
    sources = []
    if content_keywords:
        sources.append("YAKE content extraction")
    if crawl_urls:
        sources.append("Crawl inventory")
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


def load_keyword_crawl_urls(final_url: str, limit: int = 120) -> list[str]:
    website_key = site_key(final_url)
    if not website_key:
        return []
    urls: list[str] = []
    for page in list_crawl_pages(website_key, limit):
        url = str(page.get("url") or "").strip()
        content_type = str(page.get("content_type") or "").lower()
        status_code = safe_int(page.get("status_code"))
        if not url.startswith(("http://", "https://")):
            continue
        if content_type and "html" not in content_type:
            continue
        if status_code and status_code >= 400:
            continue
        urls.append(url)
    return urls


def build_keyword_source_urls(
    final_url: str, crawl_urls: list[str], sitemap_urls: list[str]
) -> list[str]:
    ordered = [final_url] + crawl_urls + sitemap_urls
    seen: set[str] = set()
    urls: list[str] = []
    for url in ordered:
        clean = str(url or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        urls.append(clean)
    return urls


def build_search_console_actions(
    rows: list[dict[str, str]],
    page_snapshots: dict[str, dict[str, str]],
    issues: list[dict[str, Any]],
) -> list[dict[str, str]]:
    actions = []
    for row in sorted(
        rows,
        key=lambda item: (
            safe_int(item.get("impressions")),
            safe_int(item.get("clicks")),
            -safe_float(item.get("position")) if item.get("position") else 0,
        ),
        reverse=True,
    ):
        query = str(row.get("query") or "").strip()
        page = str(row.get("page") or "").strip()
        if not query or not page:
            continue
        snapshot = page_snapshots.get(page) or {}
        page_issues = keyword_relevant_issues(page, issues)
        focus = keyword_focus_area(snapshot, page_issues, page)
        decision = keyword_strategy_decision(query, page, snapshot, row)
        decision_label = keyword_decision_summary(query, page, snapshot, row, page_issues)
        confidence = keyword_decision_confidence(query, page, snapshot, row, page_issues)
        content_brief = (
            keyword_supporting_content_brief(query, page, row)
            if decision == "Create supporting content"
            else {}
        )
        actions.append(
            {
                "query": query,
                "page": page,
                "clicks": row.get("clicks", "-"),
                "impressions": row.get("impressions", "-"),
                "ctr": row.get("ctr", "-"),
                "position": row.get("position", "-"),
                "focus": focus,
                "decision": decision,
                "decision_label": decision_label,
                "confidence": confidence,
                "confidence_class": keyword_confidence_class(confidence),
                "content_brief": content_brief,
                "priority": search_console_priority(row, snapshot),
                "why_now": search_console_why_now(row, snapshot, page_issues),
                "action": search_console_action(query, page, snapshot, focus),
                "current_title": snapshot.get("title", ""),
                "current_h1": snapshot.get("h1", ""),
                "current_meta": snapshot.get("meta", ""),
                "related_issues": build_related_issue_refs(page, focus, page_issues),
            }
        )
        if len(actions) >= 6:
            break
    return actions


def import_search_console_csv(upload: Any, selected_site: dict[str, str]) -> dict[str, int | str]:
    raw = upload.read(MAX_SEARCH_CONSOLE_UPLOAD_BYTES + 1)
    if not raw:
        raise ValueError("The CSV file is empty.")
    if len(raw) > MAX_SEARCH_CONSOLE_UPLOAD_BYTES:
        raise ValueError("The CSV file is larger than 2 MB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Save the CSV as UTF-8 and try again.") from exc
    if "\x00" in text:
        raise ValueError("The uploaded file is not a valid text CSV.")

    reader = csv.DictReader(io.StringIO(text))
    normalized_fields = {
        normalize_column_name(field)
        for field in (reader.fieldnames or [])
        if field
    }
    has_query_column = bool(normalized_fields.intersection({"query", "top_queries", "queries", "keyword", "search_term"}))
    has_page_column = bool(normalized_fields.intersection({"page", "top_pages", "pages", "url", "landing_page"}))
    if not has_query_column and not has_page_column:
        raise ValueError("The CSV needs a Query or Page column.")

    expected_url = str(selected_site.get("url") or "")
    expected_host = site_label(expected_url).lower().replace("www.", "")
    valid_rows: list[dict[str, str]] = []
    skipped = 0
    for index, row in enumerate(reader, start=1):
        if index > MAX_SEARCH_CONSOLE_ROWS:
            raise ValueError(f"The CSV contains more than {MAX_SEARCH_CONSOLE_ROWS:,} rows.")
        normalized = normalize_search_console_row(row)
        query = str(normalized.get("query") or "").strip()
        page = str(normalized.get("page") or "").strip()
        if not query and page:
            normalized["query"] = infer_query_from_search_console_page(page)
            normalized["source_type"] = "page"
            query = normalized["query"]
        if not query:
            skipped += 1
            continue
        if page:
            page_host = site_label(page).lower().replace("www.", "")
            if not page_host or page_host != expected_host:
                skipped += 1
                continue
        valid_rows.append(normalized)

    if not valid_rows:
        raise ValueError("No valid rows matched the selected website.")

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["Query", "Page", "Clicks", "Impressions", "CTR", "Position", "Source Type"],
        lineterminator="\n",
    )
    writer.writeheader()
    for row in valid_rows:
        writer.writerow(
            {
                "Query": row.get("query", ""),
                "Page": row.get("page", ""),
                "Clicks": row.get("clicks", ""),
                "Impressions": row.get("impressions", ""),
                "CTR": row.get("ctr", ""),
                "Position": row.get("position", ""),
                "Source Type": row.get("source_type", "query"),
            }
        )

    directory = SEARCH_CONSOLE_DIRS[0]
    filename = f"gsc-{selected_site['key']}.csv"
    target = directory / filename
    temporary = directory / f".{filename}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary.write_text(output.getvalue(), encoding="utf-8", newline="")
        temporary.replace(target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError("The Search Console storage folder is not writable.") from exc
    return {"rows": len(valid_rows), "skipped": skipped, "filename": filename}


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
        "source_type": first_present(normalized, ["source_type", "source"]),
    }


def infer_query_from_search_console_page(page: str) -> str:
    parsed = urlparse(str(page or ""))
    path = parsed.path.strip("/")
    if not path:
        host_label = parsed.netloc.split(":")[0].replace("www.", "")
        return host_label.split(".")[0].replace("-", " ").strip()
    last_segment = path.split("/")[-1]
    words = re.sub(r"[^a-zA-Z0-9]+", " ", last_segment).strip().lower()
    words = re.sub(r"\b(product|products|collection|collections|page|au)\b", "", words)
    words = re.sub(r"\s+", " ", words).strip()
    return words or path.replace("/", " ").replace("-", " ").strip().lower()


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


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value or "0").replace(",", "").replace("%", ""))
    except ValueError:
        return default


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
        "query": best.get("query", "") or "",
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


def keyword_opportunity(
    keyword: str,
    issues: list[dict[str, Any]],
    page_snapshot: dict[str, str] | None = None,
    metrics: dict[str, str] | None = None,
) -> str:
    issue_text = " ".join(str(issue.get("title") or "").lower() for issue in issues)
    page_snapshot = page_snapshot or {}
    metrics = metrics or {}
    if metrics.get("position") and safe_float(metrics.get("position")) > 8:
        return "Improve ranking on an already-impressing query"
    if not page_snapshot.get("title"):
        return "Add stronger page title"
    if not page_snapshot.get("meta"):
        return "Add stronger meta description"
    if not page_snapshot.get("h1"):
        return "Align page heading"
    if "meta description" in issue_text:
        return "Add stronger meta description"
    if "document title" in issue_text or "title" in issue_text:
        return "Improve page title"
    if "heading" in issue_text:
        return "Align H1 and headings"
    return "Strengthen copy and internal links"


def keyword_strategy_decision(
    keyword: str,
    page: str,
    page_snapshot: dict[str, str] | None = None,
    metrics: dict[str, str] | None = None,
) -> str:
    page_snapshot = page_snapshot or {}
    metrics = metrics or {}
    query = str(metrics.get("query") or "").strip()
    impressions = safe_int(metrics.get("impressions"))
    position = safe_float(metrics.get("position"))
    word_count = safe_int(page_snapshot.get("word_count"))
    intent = classify_keyword_intent(page, keyword)
    has_core_fields = bool(
        page_snapshot.get("title") and page_snapshot.get("meta") and page_snapshot.get("h1")
    )

    if (
        intent == "Informational"
        and (query or keyword_word_count(keyword) >= 3)
        and (word_count < 160 or not has_core_fields)
    ):
        return "Create supporting content"

    if query and impressions >= 100 and has_core_fields and word_count >= 220 and 1 <= position <= 6:
        return "Keep"

    if query and impressions >= 100 and (
        not page_snapshot.get("title")
        or not page_snapshot.get("meta")
        or not page_snapshot.get("h1")
    ):
        return "Improve existing page"
    if intent in {"Product", "Category", "Landing page"}:
        return "Improve existing page"
    if word_count >= 220 and has_core_fields:
        return "Keep"
    return "Create supporting content"


def keyword_supporting_content_type(
    keyword: str,
    page: str,
    metrics: dict[str, str] | None = None,
) -> str:
    metrics = metrics or {}
    phrase = f"{keyword} {metrics.get('query', '')}".lower()
    page_path = urlparse(page).path.lower()
    if any(term in phrase for term in ["how", "install", "wire", "setup", "troubleshoot"]):
        return "Create how-to guide"
    if any(term in phrase for term in ["faq", "questions", "difference", "vs", "compare", "comparison"]):
        return "Create FAQ or comparison content"
    if any(term in phrase for term in ["best", "top", "choose", "buying", "selection"]):
        return "Create buying guide"
    if "category" in page_path or "collection" in page_path:
        return "Create category support content"
    return "Create supporting content"


def keyword_supporting_content_brief(
    keyword: str,
    page: str,
    metrics: dict[str, str] | None = None,
) -> dict[str, Any]:
    metrics = metrics or {}
    seed = str(metrics.get("query") or keyword).strip() or keyword
    phrase = recommended_keyword_phrase(seed, page)
    content_type = keyword_supporting_content_type(seed, page, metrics)
    page_path = urlparse(page).path or page

    if "how-to" in content_type:
        title = f"How to use {phrase}"
        angle = "Answer setup, installation, and troubleshooting questions before sending users to the product page."
        sections = [
            f"When {phrase} is the right fit",
            "Step-by-step setup checklist",
            "Common mistakes, compatibility notes, and safety checks",
        ]
    elif "FAQ" in content_type or "comparison" in content_type:
        title = f"{phrase}: questions buyers ask before choosing"
        angle = "Capture comparison and pre-purchase questions that are too detailed for a short product/category page."
        sections = [
            f"What {phrase} is best used for",
            "How it compares with close alternatives",
            "Buying questions, fitment notes, and warranty considerations",
        ]
    elif "buying guide" in content_type:
        title = f"How to choose {phrase}"
        angle = "Turn a broad commercial keyword into a buyer guide that supports category and product pages."
        sections = [
            "Key buying criteria",
            "Recommended product types by use case",
            "Short checklist before ordering",
        ]
    elif "category" in content_type:
        title = f"{phrase} buying and fitment guide"
        angle = "Support the category page with practical guidance instead of overloading the category intro."
        sections = [
            "Best use cases",
            "Fitment and specification checklist",
            "Products or categories to link next",
        ]
    else:
        title = f"{phrase} guide"
        angle = "Create a focused support page that answers intent the current landing page does not fully cover."
        sections = [
            "What users are trying to solve",
            "Recommended options or next steps",
            "Related products, categories, or service pages",
        ]

    return {
        "type": content_type,
        "title": title,
        "angle": angle,
        "sections": sections,
        "internal_link": f"Link back to {page_path} using natural anchor text like '{phrase}'.",
        "validation": "After publishing, re-run the crawl and check whether the keyword now has a clearer target page.",
    }


def keyword_confidence_class(confidence: str) -> str:
    normalized = str(confidence or "").strip().lower()
    if "high" in normalized:
        return "high"
    if "medium" in normalized:
        return "medium"
    return "needs-data"


def keyword_decision_confidence(
    keyword: str,
    page: str,
    page_snapshot: dict[str, str] | None = None,
    metrics: dict[str, str] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> str:
    page_snapshot = page_snapshot or {}
    metrics = metrics or {}
    issues = issues or []
    query = str(metrics.get("query") or "").strip()
    impressions = safe_int(metrics.get("impressions"))
    has_core_fields = bool(
        page_snapshot.get("title") and page_snapshot.get("meta") and page_snapshot.get("h1")
    )
    decision = keyword_strategy_decision(keyword, page, page_snapshot, metrics)
    if query and impressions >= 100 and has_core_fields:
        return "High confidence"
    if query:
        return "Medium confidence"
    if decision == "Improve existing page" and page_snapshot:
        return "Medium confidence"
    if decision.startswith("Create") and keyword_word_count(keyword) >= 3:
        return "Medium confidence"
    return "Needs real search data"


def keyword_why_it_matters(
    keyword: str,
    page: str,
    page_snapshot: dict[str, str] | None = None,
    metrics: dict[str, str] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> str:
    page_snapshot = page_snapshot or {}
    metrics = metrics or {}
    issues = issues or []
    relevant_issues = keyword_relevant_issues(page, issues)
    query = str(metrics.get("query") or "").strip()
    impressions = safe_int(metrics.get("impressions"))
    position = safe_float(metrics.get("position"))
    decision = keyword_strategy_decision(keyword, page, page_snapshot, metrics)
    if decision == "Create supporting content":
        return "The current page does not look like a strong match for this search theme yet, so a supporting guide, FAQ, or category intro may serve the topic better than a small metadata edit."
    if decision == "Keep":
        return "This page already looks like a strong match for the topic, so it may be better to protect the page and focus effort elsewhere unless performance drops."
    if query and impressions >= 100:
        return f"This page already appears for '{query}', so improving the page match could turn existing visibility into more clicks."
    if position and position > 8:
        return f"This page is already showing in search around position {position:.1f}, so metadata and content improvements may unlock easier gains than building a new page."
    if not page_snapshot.get("title") or not page_snapshot.get("meta") or not page_snapshot.get("h1"):
        return "This page is missing one or more basic SEO fields, so it is a practical on-page improvement instead of a bigger content project."
    if len(relevant_issues) >= 2:
        return "This topic overlaps with existing SEO issues, so fixing the page can improve both search clarity and the site quality score."
    return f"This keyword matches the page theme, so tightening the page can make the topic clearer for both users and search engines."


def keyword_decision_summary(
    keyword: str,
    page: str,
    page_snapshot: dict[str, str] | None = None,
    metrics: dict[str, str] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> str:
    decision = keyword_strategy_decision(keyword, page, page_snapshot, metrics)
    if decision == "Create supporting content":
        return keyword_supporting_content_type(keyword, page, metrics)
    return decision


def keyword_edit_brief(keyword: str, page: str, focus: str, decision: str) -> dict[str, str]:
    phrase = recommended_keyword_phrase(keyword, page)
    if decision == "Keep":
        return {
            "title": "Protect the current page",
            "do": "Keep the core title, H1, and intent stable. Only make small copy improvements if the scan finds a clear issue.",
            "avoid": "Do not rewrite a page that already has a clear topic unless rankings, clicks, or issue counts drop.",
            "validation": "Re-scan after any small edit and confirm the score does not regress.",
        }
    if decision == "Create supporting content":
        return {
            "title": "Create a clearer target for this topic",
            "do": f"Draft a focused support page or section for '{phrase}', then link it from the closest category or product page.",
            "avoid": "Do not force a broad informational query into a short product page if the intent needs explanation.",
            "validation": "Re-scan after publishing and check that the new page appears in the keyword-to-page map.",
        }
    if focus == "Title tag":
        return {
            "title": "Rewrite the title first",
            "do": f"Lead the title with '{phrase}', then add the product/category benefit and brand if there is room.",
            "avoid": "Avoid repeating the same phrase twice or writing a title that no longer matches the visible page.",
            "validation": "The title is unique, under roughly 60 characters, and matches the H1/page intent.",
        }
    if focus == "Meta description":
        return {
            "title": "Improve the search snippet",
            "do": f"Write a concise meta description that uses '{phrase}' once and explains why this page is useful.",
            "avoid": "Avoid keyword stuffing or a generic description that could apply to any page.",
            "validation": "The meta description is unique, useful, and roughly 120-160 characters.",
        }
    if focus == "Heading structure":
        return {
            "title": "Align the visible heading",
            "do": f"Make the H1 clearly describe '{phrase}' and keep supporting headings in a logical order.",
            "avoid": "Avoid multiple competing H1s or headings that look decorative but do not describe the content.",
            "validation": "The scan no longer reports heading order or missing H1 issues for this page.",
        }
    if focus == "Supporting copy":
        return {
            "title": "Add useful body copy",
            "do": f"Add a short intro or buying note that explains '{phrase}' in plain language before the product grid.",
            "avoid": "Avoid thin copy that only repeats keywords without helping the buyer choose.",
            "validation": "The page has enough unique text to explain the topic and support internal links.",
        }
    return {
        "title": "Strengthen topical signals",
        "do": f"Use '{phrase}' naturally in the most visible page fields and link from related pages.",
        "avoid": "Avoid creating duplicate pages that target the same phrase without a clear purpose.",
        "validation": "The page has a clear target phrase, unique metadata, and no new quality issues.",
    }


def keyword_score_gain(
    keyword: str,
    count: int,
    affected_pages: int,
    issues: list[dict[str, Any]],
    metrics: dict[str, str] | None = None,
) -> float:
    metrics = metrics or {}
    issue_bonus = min(len(issues) * 0.05, 0.7)
    page_bonus = min(affected_pages * 0.06, 1.2)
    keyword_bonus = 0.25 if len(keyword) >= 6 else 0.1
    impressions_bonus = min(safe_int(metrics.get("impressions")) / 500, 0.9)
    position_bonus = 0.45 if 4 <= safe_float(metrics.get("position")) <= 20 else 0
    return round(0.35 + page_bonus + issue_bonus + keyword_bonus + impressions_bonus + position_bonus + min(count * 0.03, 0.5), 2)


def keyword_difficulty(
    keyword: str,
    affected_pages: int,
    issues: list[dict[str, Any]],
    metrics: dict[str, str] | None = None,
) -> str:
    metrics = metrics or {}
    if safe_int(metrics.get("impressions")) >= 500 and safe_float(metrics.get("position")) > 12:
        return "Medium"
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


def keyword_action(
    keyword: str,
    page: str,
    page_snapshot: dict[str, str] | None = None,
    metrics: dict[str, str] | None = None,
) -> str:
    label = recommended_keyword_phrase(keyword, page)
    page_snapshot = page_snapshot or {}
    metrics = metrics or {}
    decision = keyword_strategy_decision(keyword, page, page_snapshot, metrics)
    if decision == "Create supporting content":
        return f"Create a supporting content block or new landing page for '{label}', then link it clearly from this page so the topic has a stronger home."
    if decision == "Keep":
        return f"Keep this page stable for '{label}', monitor search performance, and only make small refinements if rankings or clicks decline."
    if metrics.get("query"):
        return f"Treat '{metrics['query']}' as the live search phrase to support, then update the title, H1, meta description and intro copy on this page."
    if not page_snapshot.get("title"):
        return f"Add a page title that leads with '{label}' and matches what users expect to find on this page."
    if not page_snapshot.get("meta"):
        return f"Write a meta description for this page that uses '{label}' once and clearly states the page benefit."
    if not page_snapshot.get("h1"):
        return f"Add a clear H1 using '{label}' or a close variation that matches the page purpose."
    if classify_keyword_intent(page, keyword) == "Product":
        return f"Use '{label}' in the product title, first paragraph, image alt text and related-product links where accurate."
    if classify_keyword_intent(page, keyword) == "Category":
        return f"Use '{label}' in the category intro, title tag, H1 and internal links from related pages."
    return f"Use '{label}' naturally in the page title, H1, meta description and one useful supporting paragraph."


def keyword_relevant_issues(
    page: str,
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not page:
        return issues
    matched: list[dict[str, Any]] = []
    unscoped: list[dict[str, Any]] = []
    for issue in issues:
        normalized = normalize_issue_detail(issue)
        if not normalized:
            continue
        examples = list(normalized.get("affected_examples") or [])
        affected_urls = list(normalized.get("affected_urls") or [])
        has_page_evidence = bool(
            affected_urls
            or any(str(example.get("page_url") or example.get("url") or "").strip() for example in examples)
        )
        if has_page_evidence:
            if issue_matches_page(page, normalized):
                matched.append(issue)
        else:
            unscoped.append(issue)
    return matched or unscoped


def keyword_focus_area(
    page_snapshot: dict[str, str],
    issues: list[dict[str, Any]],
    page: str = "",
) -> str:
    relevant_issues = keyword_relevant_issues(page, issues)
    issue_text = " ".join(str(issue.get("title") or "").lower() for issue in relevant_issues)
    if not page_snapshot.get("title") or "document title" in issue_text:
        return "Title tag"
    if not page_snapshot.get("meta") or "meta description" in issue_text:
        return "Meta description"
    if not page_snapshot.get("h1") or "heading" in issue_text:
        return "Heading structure"
    if safe_int(page_snapshot.get("word_count")) < 180:
        return "Supporting copy"
    return "Internal linking"


def keyword_why_now(
    keyword: str,
    page: str,
    metrics: dict[str, str],
    page_snapshot: dict[str, str],
    issues: list[dict[str, Any]],
) -> str:
    relevant_issues = keyword_relevant_issues(page, issues)
    query = metrics.get("query", "")
    impressions = safe_int(metrics.get("impressions"))
    position = safe_float(metrics.get("position"))
    if query and impressions >= 100:
        return f"Search Console already shows '{query}' with {impressions} impression(s), so this is not just a guess."
    if position and position > 8:
        return f"This page is already visible around position {position:.1f}, so better metadata or copy could move it higher."
    if not page_snapshot.get("title") or not page_snapshot.get("meta") or not page_snapshot.get("h1"):
        return "This page is missing basic SEO structure, so it is a good candidate for a first-pass content update."
    if len(relevant_issues) >= 2:
        return "This page theme overlaps with existing SEO issues, so fixing it can improve both quality score and search clarity."
    return f"The phrase '{recommended_keyword_phrase(keyword, page)}' matches the page theme and can make the page clearer for both users and search engines."


def keyword_priority_sort_key(row: dict[str, str]) -> tuple[float, int, int]:
    return (
        safe_float(row.get("score_gain")),
        safe_int(row.get("impressions")),
        -safe_float(row.get("position")) if row.get("position") not in {"", "-"} else 0,
    )


def search_console_priority(row: dict[str, str], snapshot: dict[str, str]) -> str:
    impressions = safe_int(row.get("impressions"))
    position = safe_float(row.get("position"))
    if impressions >= 500 and position >= 5:
        return "Highest impact"
    if not snapshot.get("title") or not snapshot.get("meta") or not snapshot.get("h1"):
        return "Quick win"
    if impressions >= 100:
        return "Promising"
    return "Review"


def search_console_why_now(
    row: dict[str, str],
    snapshot: dict[str, str],
    issues: list[dict[str, Any]],
) -> str:
    query = str(row.get("query") or "")
    impressions = safe_int(row.get("impressions"))
    clicks = safe_int(row.get("clicks"))
    position = safe_float(row.get("position"))
    if not snapshot.get("title") or not snapshot.get("meta") or not snapshot.get("h1"):
        return f"'{query}' already has real search visibility, and this landing page is still missing one or more core SEO fields."
    if impressions >= 500 and position >= 8:
        return f"'{query}' already generates {impressions} impression(s) and sits around position {position:.1f}, so a tighter page match could lift traffic."
    if clicks == 0 and impressions >= 100:
        return f"'{query}' is showing in search but not earning clicks yet, which usually means the page snippet or page match can improve."
    if len(issues) >= 3:
        return f"This page also overlaps with current SEO issues, so improving it for '{query}' should strengthen both relevance and site quality."
    return f"'{query}' is a real query from Search Console, so this recommendation is grounded in search demand instead of guesswork."


def search_console_action(
    query: str,
    page: str,
    snapshot: dict[str, str],
    focus: str,
) -> str:
    phrase = recommended_keyword_phrase(query, page)
    if focus == "Title tag":
        return f"Rewrite the page title so it leads with '{phrase}' and clearly matches the searcher intent behind '{query}'."
    if focus == "Meta description":
        return f"Write a more compelling meta description using '{phrase}' once and making the page benefit clearer."
    if focus == "Heading structure":
        return f"Align the H1 and visible page heading with '{phrase}' so the page topic is obvious to users and search engines."
    if focus == "Supporting copy":
        return f"Add a stronger intro section for '{phrase}' so the landing page better answers the query without stuffing keywords."
    return f"Add internal links to this page using descriptive anchor text related to '{phrase}', especially from closely related pages."


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


def keyword_word_count(value: str) -> int:
    return len(keyword_tokens(value))


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
    pages: list[dict[str, Any]], rows: list[dict[str, str]], brand_name: str
) -> list[dict[str, Any]]:
    row_by_page = {row.get("page", ""): row for row in rows}
    briefs = []
    for item in pages[:6]:
        page = item.get("page", "")
        base_keyword = item.get("suggested_keyword", "")
        keyword = page_specific_keyword_phrase(
            base_keyword,
            page,
            {"h1": item.get("current_h1", "")},
        )
        intent = item.get("intent") or classify_keyword_intent(page, base_keyword)
        page_type = "collection" if intent == "Category" else "product" if intent == "Product" else "landing"
        row = row_by_page.get(page, {})
        decision = item.get("decision", row.get("decision", "Improve existing page"))
        decision_label = item.get(
            "decision_label",
            row.get("decision_label", decision),
        )
        confidence = item.get("confidence", row.get("confidence", "Needs real search data"))
        focus = item.get("focus", row.get("focus", "On-page SEO"))
        content_brief = item.get("content_brief") or row.get("content_brief") or (
            keyword_supporting_content_brief(keyword, page, {"query": row.get("matched_query", "")})
            if decision == "Create supporting content"
            else {}
        )
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
                "matched_query": row.get("matched_query", ""),
                "why_now": item.get("why_now", row.get("why_now", "")),
                "why_it_matters": item.get("why_it_matters", row.get("why_it_matters", "")),
                "current_title": item.get("current_title", ""),
                "current_h1": item.get("current_h1", ""),
                "current_meta": item.get("current_meta", ""),
                "focus": focus,
                "decision": decision,
                "decision_label": decision_label,
                "confidence": confidence,
                "confidence_class": keyword_confidence_class(confidence),
                "content_brief": content_brief,
                "edit_brief": keyword_edit_brief(keyword, page, focus, decision),
                "title": seo_title_example(keyword, brand_name),
                "h1": seo_h1_example(keyword),
                "meta": seo_meta_example(keyword, page_type, brand_name),
                "intro": seo_intro_example(keyword, page_type),
                "alt": seo_alt_example(keyword),
                "internal_links": seo_internal_link_example(keyword, page_type),
                "related_issues": item.get("related_issues", []),
            }
        )
    return briefs


def build_page_edit_queue(briefs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue = []
    for brief in briefs[:6]:
        tasks = []
        current_title = str(brief.get("current_title") or "").strip()
        current_h1 = str(brief.get("current_h1") or "").strip()
        current_meta = str(brief.get("current_meta") or "").strip()

        if not current_title or current_title != brief.get("title", ""):
            suggested_title = str(brief.get("title", "") or "")
            tasks.append(
                {
                    "label": "Title tag",
                    "current": current_title or "Missing",
                    "suggested": suggested_title,
                    "guidance": "Copy this into the page SEO title field.",
                    "metric": f"{len(suggested_title)} characters",
                }
            )
        if not current_h1 or current_h1 != brief.get("h1", ""):
            suggested_h1 = str(brief.get("h1", "") or "")
            tasks.append(
                {
                    "label": "H1",
                    "current": current_h1 or "Missing",
                    "suggested": suggested_h1,
                    "guidance": "Use this as the visible main heading if it still reads naturally.",
                    "metric": f"{len(suggested_h1)} characters",
                }
            )
        if not current_meta or current_meta != brief.get("meta", ""):
            suggested_meta = str(brief.get("meta", "") or "")
            tasks.append(
                {
                    "label": "Meta description",
                    "current": current_meta or "Missing",
                    "suggested": suggested_meta,
                    "guidance": "Copy this into the CMS meta description or SEO description field.",
                    "metric": f"{len(suggested_meta)} characters",
                    "important": True,
                }
            )

        suggested_intro = str(brief.get("intro", "") or "")
        tasks.append(
            {
                "label": "Intro copy",
                "current": "Needs editorial review",
                "suggested": suggested_intro,
                "guidance": "Add this as a short visible paragraph near the top of the page, then rewrite in your brand voice.",
                "metric": "Visible page copy",
            }
        )

        queue.append(
            {
                "page": brief.get("page", ""),
                "keyword": brief.get("keyword", ""),
                "priority": brief.get("priority", "Review"),
                "points": brief.get("points", "0"),
                "focus": brief.get("focus", "On-page SEO"),
                "why_now": brief.get("why_now", ""),
                "why_it_matters": brief.get("why_it_matters", ""),
                "decision": brief.get("decision", "Improve existing page"),
                "decision_label": brief.get("decision_label", brief.get("decision", "Improve existing page")),
                "confidence": brief.get("confidence", "Needs real search data"),
                "confidence_class": keyword_confidence_class(brief.get("confidence", "Needs real search data")),
                "content_brief": brief.get("content_brief", {}),
                "edit_brief": brief.get("edit_brief", {}),
                "matched_query": brief.get("matched_query", ""),
                "tasks": tasks[:4],
                "related_issues": list(brief.get("related_issues") or [])[:3],
            }
        )
    return queue


def apply_keyword_action_states(
    website_key_value: str,
    *collections: list[dict[str, Any]],
) -> dict[str, Any]:
    saved_actions = {
        str(action.get("action_key") or ""): action
        for action in list_keyword_actions(website_key_value)
        if action.get("action_key")
    } if website_key_value else {}
    visible_states: dict[str, str] = {}
    for collection in collections:
        for item in collection:
            page_url = str(item.get("page") or item.get("page_url") or "").strip()
            keyword = str(item.get("keyword") or item.get("suggested_keyword") or "").strip()
            if not page_url or not keyword:
                continue
            action_key_value = keyword_action_key(page_url, keyword)
            saved = saved_actions.get(action_key_value) or {}
            workflow = {
                "action_key": action_key_value,
                "status": saved.get("status", "suggested"),
                "owner": saved.get("owner", "Unassigned"),
                "note": saved.get("note", ""),
                "updated_at": saved.get("updated_at", ""),
                "completed_at": saved.get("completed_at", ""),
            }
            item["workflow"] = workflow
            visible_states[action_key_value] = str(workflow["status"])

    counts = Counter(visible_states.values())
    return {
        "saved": len(saved_actions),
        "visible": len(visible_states),
        "suggested": counts.get("suggested", 0),
        "accepted": counts.get("accepted", 0),
        "in_progress": counts.get("in_progress", 0),
        "completed": counts.get("completed", 0),
        "ignored": counts.get("ignored", 0),
    }


def build_related_issue_refs(
    page_url: str, focus: str, issues: list[dict[str, Any]], limit: int = 3
) -> list[dict[str, str]]:
    page_url = str(page_url or "").strip()
    focus_tokens = related_issue_focus_tokens(focus)
    ranked: list[tuple[float, dict[str, str]]] = []
    seen: set[str] = set()

    for issue in issues:
        normalized = normalize_issue_detail(issue)
        if not normalized:
            continue
        if not related_issue_is_allowed(normalized, focus_tokens):
            continue

        page_match = issue_matches_page(page_url, normalized)
        focus_match = issue_matches_focus(focus_tokens, normalized)
        if not page_match and not focus_match:
            continue

        issue_id = str(
            normalized.get("id")
            or normalized.get("audit_id")
            or normalized.get("issue_key")
            or ""
        )
        if not issue_id or issue_id in seen:
            continue
        seen.add(issue_id)

        points = safe_float(normalized.get("points"), 0.0)
        score = points
        if page_match:
            score += 100
        if focus_match:
            score += 25

        ranked.append(
            (
                score,
                {
                    "id": issue_id,
                    "title": str(normalized.get("title") or "Untitled issue"),
                    "category": str(normalized.get("category") or "General"),
                    "points": f"{points:.1f}",
                    "href": str(normalized.get("href") or issue_href(issue_id, page_url or None)),
                    "match": "This page" if page_match else "Related issue",
                },
            )
        )

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in ranked[:limit]]


def related_issue_focus_tokens(focus: str) -> set[str]:
    focus = str(focus or "").strip().lower()
    mapping = {
        "title tag": {"document-title", "title", "browser title", "page title"},
        "meta description": {"meta-description", "meta description", "description"},
        "heading structure": {"heading-order", "heading", "multiple h1", "missing h1", "h1"},
        "supporting copy": {"thin content", "content", "word count", "copy", "readability"},
        "image alt": {"image-alt", "alt text", "image"},
        "internal links": {"link-name", "link", "anchor", "internal link"},
        "on-page seo": {
            "document-title",
            "meta-description",
            "heading-order",
            "missing h1",
            "multiple h1",
            "thin content",
            "link-name",
            "image-alt",
            "title",
            "meta",
            "heading",
            "copy",
        },
    }
    return mapping.get(focus, {focus} if focus else set())


def related_issue_is_allowed(issue: dict[str, Any], focus_tokens: set[str]) -> bool:
    category = str(issue.get("category") or "").strip().lower()
    issue_id = str(issue.get("id") or issue.get("audit_id") or issue.get("issue_key") or "").lower()
    source = str(issue.get("source") or "").strip().lower()
    allowed_categories = {"seo", "content quality", "accessibility"}
    allowed_sources = {"content", "pa11y", "lighthouse", "lhci"}
    allowed_issue_tokens = {
        "document-title",
        "meta-description",
        "heading-order",
        "multiple-h1",
        "missing-h1",
        "link-name",
        "image-alt",
        "html-has-lang",
        "content-thin-page",
        "content-missing-title",
        "content-title-length",
        "content-duplicate-title",
        "content-missing-meta-description",
        "content-meta-length",
        "content-duplicate-meta-description",
        "content-missing-h1",
        "content-multiple-h1",
        "content-missing-language",
    }
    if category in allowed_categories:
        return True
    if source in allowed_sources and any(token in issue_id for token in allowed_issue_tokens):
        return True
    return any(token in issue_id for token in focus_tokens if token)


def issue_matches_focus(tokens: set[str], issue: dict[str, Any]) -> bool:
    if not tokens:
        return False
    haystack = " ".join(
        [
            str(issue.get("id") or ""),
            str(issue.get("audit_id") or ""),
            str(issue.get("issue_key") or ""),
            str(issue.get("title") or ""),
            str(issue.get("category") or ""),
            str(issue.get("recommendation") or ""),
        ]
    ).lower()
    return any(token in haystack for token in tokens if token)


def issue_matches_page(page_url: str, issue: dict[str, Any]) -> bool:
    if not page_url:
        return False
    normalized_page = page_url.rstrip("/").lower()
    candidates = [str(issue.get("page_url") or "")]
    for collection_name in ("affected_examples", "evidence"):
        for item in issue.get(collection_name, []) or []:
            if isinstance(item, dict):
                candidates.append(str(item.get("page_url") or item.get("url") or ""))
    return any(candidate.rstrip("/").lower() == normalized_page for candidate in candidates if candidate)


def seo_title_example(keyword: str, brand_name: str) -> str:
    return compact_text(f"{keyword} | {brand_name}", 62)


def seo_h1_example(keyword: str) -> str:
    return keyword[:1].upper() + keyword[1:] if keyword else keyword


def seo_meta_example(keyword: str, page_type: str, brand_name: str) -> str:
    if page_type == "collection":
        text = f"Shop {keyword} for trucks, trailers and commercial vehicles, with durable automotive lighting options from {brand_name}."
    elif page_type == "product":
        text = f"View {keyword} specs, voltage range and fitment details for reliable automotive and commercial vehicle lighting from {brand_name}."
    else:
        text = f"Learn about {keyword}, compare suitable options and find reliable {brand_name} lighting for your vehicle or fleet."
    return complete_sentence(text, 155)


def complete_sentence(text: str, limit: int) -> str:
    """Keep CMS-ready copy readable; never end SEO text with a broken word or ellipsis."""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned if cleaned.endswith((".", "!", "?")) else f"{cleaned}."
    shortened = cleaned[: max(limit - 1, 0)].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{shortened}."


def seo_intro_example(keyword: str, page_type: str) -> str:
    if page_type == "collection":
        return f"Add a short intro above the product grid explaining who {keyword} are for, common vehicle uses, voltage range, durability and why customers should choose this range."
    if page_type == "product":
        return f"Add one paragraph near the top explaining who this product is for, where {keyword} is used, the key specifications, installation context and compatible vehicle applications."
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
    urls: list[str],
    issues: list[dict[str, Any]],
    search_rows: list[dict[str, str]],
    page_snapshots: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    rows = []
    seen_pages = set()
    for url in urls:
        if url in seen_pages:
            continue
        seen_pages.add(url)
        snapshot = page_snapshots.get(url, {})
        phrase = page_specific_keyword_phrase(suggested_phrase_from_url(url), url, snapshot)
        if not phrase:
            continue
        matched_metrics = match_search_console_metrics(phrase, url, search_rows)
        page_issues = keyword_relevant_issues(url, issues)
        current_title = snapshot.get("title", "")
        current_meta = snapshot.get("meta", "")
        current_h1 = snapshot.get("h1", "")
        decision = keyword_strategy_decision(phrase, url, snapshot, matched_metrics)
        decision_label = keyword_decision_summary(phrase, url, snapshot, matched_metrics, page_issues)
        confidence = keyword_decision_confidence(phrase, url, snapshot, matched_metrics, page_issues)
        focus = keyword_focus_area(snapshot, page_issues, url)
        content_brief = (
            keyword_supporting_content_brief(phrase, url, matched_metrics)
            if decision == "Create supporting content"
            else {}
        )
        rows.append(
            {
                "page": url,
                "suggested_keyword": phrase,
                "intent": classify_keyword_intent(url, phrase.split()[0]),
                "decision": decision,
                "decision_label": decision_label,
                "confidence": confidence,
                "confidence_class": keyword_confidence_class(confidence),
                "content_brief": content_brief,
                "priority": page_keyword_priority(url, page_issues, matched_metrics, snapshot),
                "action": keyword_action(phrase, url, snapshot, matched_metrics),
                "focus": focus,
                "why_now": keyword_why_now(phrase, url, matched_metrics, snapshot, page_issues),
                "why_it_matters": keyword_why_it_matters(phrase, url, snapshot, matched_metrics, page_issues),
                "matched_query": matched_metrics.get("query", ""),
                "current_title": current_title,
                "current_meta": current_meta,
                "current_h1": current_h1,
                "current_state": page_keyword_current_state(snapshot),
                "related_issues": build_related_issue_refs(url, focus, page_issues),
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


def page_keyword_priority(
    url: str,
    issues: list[dict[str, Any]],
    metrics: dict[str, str] | None = None,
    snapshot: dict[str, str] | None = None,
) -> str:
    issue_text = " ".join(str(issue.get("title") or "").lower() for issue in issues)
    metrics = metrics or {}
    snapshot = snapshot or {}
    if safe_int(metrics.get("impressions")) >= 300 and safe_float(metrics.get("position")) >= 6:
        return "Highest impact"
    if not snapshot.get("title") or not snapshot.get("meta") or not snapshot.get("h1"):
        return "Quick win"
    if any(term in issue_text for term in ["meta description", "document title", "heading"]):
        return "High"
    if any(term in urlparse(url).path.lower() for term in ["product", "collection", "category"]):
        return "Medium"
    return "Review"


def page_keyword_current_state(snapshot: dict[str, str]) -> str:
    missing = []
    if not snapshot.get("title"):
        missing.append("title")
    if not snapshot.get("meta"):
        missing.append("meta description")
    if not snapshot.get("h1"):
        missing.append("H1")
    if missing:
        return "Missing " + ", ".join(missing)
    if safe_int(snapshot.get("word_count")) < 180:
        return "Thin copy"
    return "Has core SEO fields"


def build_keyword_cannibalization(pages: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for row in pages:
        phrase = str(row.get("suggested_keyword") or "").strip().lower()
        page = str(row.get("page") or "")
        if not phrase or not page:
            continue
        grouped.setdefault(phrase, []).append(page)
    warnings = []
    for phrase, urls in grouped.items():
        if len(urls) > 1:
            warnings.append(
                {
                    "keyword": phrase,
                    "pages": urls,
                    "count": len(urls),
                    "action": "Choose one primary landing page for this phrase and adjust titles, H1s, and internal links so the other pages support it instead of competing.",
                }
            )
    return warnings[:6]


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


def build_content_governance_summary(pages: list[dict[str, Any]], issues: list[dict[str, Any]], website_key: str) -> dict[str, Any]:
    content_issues = [issue for issue in issues if issue.get("source") == "content"]
    open_issues = [issue for issue in content_issues if issue.get("status") not in {"resolved", "ignored"}]
    affected_urls = {
        str(evidence.get("page_url") or "")
        for issue in open_issues
        for evidence in issue.get("evidence", [])
        if evidence.get("page_url")
    }
    rows = []
    for issue in sorted(open_issues, key=lambda item: float(item.get("points") or 0), reverse=True):
        evidence = next((entry for entry in issue.get("evidence", []) if entry.get("page_url")), {})
        page_url = str(evidence.get("page_url") or "")
        rows.append({
            **issue,
            "page_url": page_url,
            "page_href": f"/pages/inspect?site={quote(website_key, safe='')}&url={quote(page_url, safe='')}" if page_url else "",
            "example": evidence,
        })
    definitions = [
        ("Titles", {"content-missing-title", "content-title-length", "content-duplicate-title"}),
        ("Descriptions", {"content-missing-meta-description", "content-meta-length", "content-duplicate-meta-description"}),
        ("Heading structure", {"content-missing-h1", "content-multiple-h1"}),
        ("Content depth", {"content-thin-page"}),
        ("Page language", {"content-missing-language"}),
    ]
    checks = []
    for name, audit_ids in definitions:
        matching = [issue for issue in open_issues if issue.get("audit_id") in audit_ids]
        checks.append({"name": name, "count": len(matching), "status": "Needs attention" if matching else "Passed"})
    deduction = sum(float(issue.get("points") or 0) * max(1, int(issue.get("affected_pages") or 1)) for issue in open_issues)
    score = max(0, min(100, round(100 - deduction * 1.5))) if pages else 0
    return {
        "status": "Needs attention" if open_issues else "Passed" if pages else "Run needed",
        "score": score,
        "issue_count": len(open_issues),
        "affected_pages": len(affected_urls),
        "scanned_pages": len([page for page in pages if "html" in str(page.get("content_type") or "").lower()]),
        "checks": checks,
        "rows": rows,
        "connector_note": "Built-in checks cover metadata, headings, language, duplicates, and thin content. LanguageTool or Vale can later add dictionary, tone, and editorial-policy rules.",
    }


def build_technical_crawl_summary(pages: list[dict[str, Any]], issues: list[dict[str, Any]]) -> dict[str, Any]:
    html_pages = [page for page in pages if "html" in str(page.get("content_type") or "").lower()]
    rows = []
    schema_types: dict[str, int] = {}
    totals = {
        "pages": len(html_pages), "indexable": 0, "canonical": 0,
        "structured": 0, "open_graph": 0, "hreflang": 0,
        "hsts": 0, "csp": 0, "nosniff": 0, "referrer": 0,
    }
    for page in html_pages:
        data = page.get("technical_data") if isinstance(page.get("technical_data"), dict) else {}
        headers = data.get("security_headers") if isinstance(data.get("security_headers"), dict) else {}
        open_graph = data.get("open_graph") if isinstance(data.get("open_graph"), dict) else {}
        types = [str(value) for value in data.get("structured_data_types", []) if value]
        indexable = bool(data.get("indexable", True))
        totals["indexable"] += int(indexable)
        totals["canonical"] += int(bool(data.get("canonical")))
        totals["structured"] += int(bool(types))
        totals["open_graph"] += int(bool(open_graph.get("title") and open_graph.get("description")))
        totals["hreflang"] += int(bool(data.get("hreflang")))
        totals["hsts"] += int(bool(headers.get("strict_transport_security")))
        totals["csp"] += int(bool(headers.get("content_security_policy")))
        totals["nosniff"] += int(bool(headers.get("x_content_type_options")))
        totals["referrer"] += int(bool(headers.get("referrer_policy")))
        for schema_type in types:
            schema_types[schema_type] = schema_types.get(schema_type, 0) + 1
        rows.append({
            "url": page.get("url"), "title": page.get("title") or "Untitled page",
            "indexable": indexable, "robots": data.get("robots") or "Default: index, follow",
            "canonical": data.get("canonical") or "Missing", "canonical_ok": bool(data.get("canonical")) and int(data.get("canonical_count") or 0) == 1,
            "schema_types": types, "invalid_json_ld": int(data.get("invalid_json_ld") or 0),
            "open_graph_ok": bool(open_graph.get("title") and open_graph.get("description")),
            "headers": headers,
        })
    technical_issues = [issue for issue in issues if issue.get("status") not in {"resolved", "ignored"} and issue.get("category") in {"Technical SEO", "Security"}]
    denominator = max(1, len(html_pages))
    coverage = {key: round(value / denominator * 100) for key, value in totals.items() if key != "pages"}
    return {
        "totals": totals, "coverage": coverage, "rows": rows,
        "schema_types": [{"name": name, "pages": count} for name, count in sorted(schema_types.items(), key=lambda item: (-item[1], item[0]))],
        "issues": technical_issues, "issue_count": len(technical_issues),
        "status": "Needs attention" if technical_issues else "Healthy" if html_pages else "Run needed",
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


def build_link_integrity_summary(
    reports: list[dict[str, str]],
    crawl_pages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    crawl_pages = crawl_pages or []
    link_reports = [
        report for report in reports if "link" in report.get("name", "").lower()
    ]
    link_data = load_latest_linkcheck_report(link_reports)
    error_count = int(link_data.get("error_count") or 0) if link_data else 0
    warning_count = int(link_data.get("warning_count") or 0) if link_data else 0
    checked_count = int(link_data.get("checked_count") or 0) if link_data else 0
    inventory_findings = crawl_link_findings(crawl_pages)
    inventory_error_count = len(inventory_findings)
    effective_error_count = error_count or inventory_error_count
    effective_checked_count = checked_count or len(crawl_pages)
    source_label = "LinkChecker report" if link_reports else "Crawl inventory" if crawl_pages else "Not checked yet"
    status = (
        "Needs fixes"
        if effective_error_count
        else "Report available"
        if link_reports
        else "Inventory available"
        if crawl_pages
        else "Run needed"
    )
    return {
        "status": status,
        "report_count": len(link_reports),
        "reports": link_reports[:6],
        "error_count": str(effective_error_count),
        "warning_count": str(warning_count),
        "checked_count": str(effective_checked_count),
        "source_label": source_label,
        "inventory_error_count": str(inventory_error_count),
        "script_command": r"powershell -ExecutionPolicy Bypass -File .\scripts\run-linkcheck.ps1 -TargetUrl https://example.com",
        "sample_errors": (link_data or {}).get("sample_errors", []) or [item["summary"] for item in inventory_findings[:8]],
        "inventory_findings": inventory_findings[:12],
        "checks": [
            {
                "name": "Broken links",
                "status": "Needs fixes" if effective_error_count else "Passed" if effective_checked_count else "Run LinkChecker",
                "detail": f"{effective_error_count} issue(s), {warning_count} warning(s), {effective_checked_count} checked page/link record(s).",
            },
            {
                "name": "Reachability source",
                "status": source_label,
                "detail": "LinkChecker provides full link-level crawling; crawl inventory provides page-level HTTP status coverage.",
            },
            {
                "name": "Fix ownership",
                "status": "Ready" if effective_error_count else "Waiting for issues",
                "detail": "Assign 404/500 pages to content owners, template owners, or redirect owners based on URL pattern.",
            },
        ],
    }


def crawl_link_findings(crawl_pages: list[dict[str, Any]]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for page in crawl_pages:
        url = str(page.get("url") or "")
        status_text = str(page.get("status_code") or "").strip()
        error = str(page.get("error") or "").strip()
        status = safe_int(status_text)
        if status < 400 and not error:
            continue
        if status >= 500:
            severity = "High"
            action = "Check hosting, application errors, and server logs; restore the page or redirect it."
            fix_steps = [
                "Open the URL and confirm whether the server error still happens.",
                "Check hosting, application logs, or Shopify/app configuration for this route.",
                "Restore the page if it should exist, or redirect it to the closest useful page.",
            ]
        elif status >= 400:
            severity = "Medium"
            action = "Restore the missing page, update internal links, or add a relevant 301 redirect."
            fix_steps = [
                "Open the URL to confirm whether the page is genuinely missing.",
                "If the product/page was removed, create a 301 redirect to the closest live replacement.",
                "Update internal links, menu links, sitemap entries, and campaign links that still point here.",
            ]
        else:
            severity = "Medium"
            action = "Investigate the crawler error, timeout, DNS, SSL, or blocking rule."
            fix_steps = [
                "Open the URL in a browser and check whether it loads consistently.",
                "Check DNS, SSL, robots/firewall rules, and crawler blocking.",
                "Run LinkChecker to confirm whether this is a temporary crawl issue or a real broken link.",
            ]
        findings.append({
            "url": url,
            "status": status_text or "Error",
            "source": str(page.get("source") or "crawl"),
            "severity": severity,
            "action": action,
            "fix_steps": fix_steps,
            "summary": f"{status_text or 'Error'} {url} {error}".strip(),
        })
    return findings


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
    data: dict[str, Any],
    audits: dict[str, Any],
    reports: list[dict[str, str]],
    crawl_pages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    crawl_pages = crawl_pages or []
    sitemap_reports = [
        report for report in reports if "sitemap" in report.get("name", "").lower()
    ]
    sitemap_reports = enrich_sitemap_reports_for_site(sitemap_reports, data, crawl_pages)
    sitemap_data = load_latest_sitemap_report(sitemap_reports)
    sitemap_url_count = int(sitemap_data.get("url_count") or 0) if sitemap_data else 0
    sitemap_error_count = int(sitemap_data.get("error_count") or 0) if sitemap_data else 0
    sitemap_count = int(sitemap_data.get("sitemap_count") or 0) if sitemap_data else 0
    crawl_sitemap_pages = [page for page in crawl_pages if str(page.get("source") or "") == "sitemap"]
    discovered_count = sitemap_url_count or len(crawl_sitemap_pages) or len(crawl_pages)
    sitemap_source_label = "XML sitemap report" if sitemap_data else "Crawl inventory" if crawl_pages else "Not discovered yet"
    sitemap_files = [
        {"url": str(url), "kind": sitemap_file_kind(str(url))}
        for url in (sitemap_data or {}).get("sitemaps", [])[:8]
    ]
    page_samples = (sitemap_data or {}).get("urls", [])[:10]
    if not page_samples and crawl_pages:
        page_samples = [
            {
                "loc": page.get("url", ""),
                "lastmod": "",
                "changefreq": str(page.get("source") or ""),
                "priority": "",
            }
            for page in crawl_pages[:10]
        ]
    sitemap_status = "Report available" if sitemap_data else "Inventory available" if crawl_pages else "Run needed"
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
            "status": sitemap_status,
            "reports": sitemap_reports[:6],
            "final_url": data.get("finalDisplayedUrl") or data.get("finalUrl") or "",
            "url_count": str(discovered_count),
            "sitemap_count": str(sitemap_count),
            "error_count": str(sitemap_error_count),
            "sample_pages": page_samples,
            "errors": (sitemap_data or {}).get("errors", [])[:5],
            "generated_at": format_date(str((sitemap_data or {}).get("generated_at", ""))),
            "source": (sitemap_data or {}).get("sitemap_entry", ""),
            "sitemap_files": sitemap_files,
            "inventory_pages": str(len(crawl_pages)),
            "sitemap_inventory_pages": str(len(crawl_sitemap_pages)),
            "source_label": sitemap_source_label,
            "script_command": r"powershell -ExecutionPolicy Bypass -File .\scripts\crawl-sitemaps.ps1",
            "scan_href": "/scans",
            "next_step": "Use discovered pages as the crawl queue for Lighthouse, Pa11y, LinkChecker and content checks.",
        },
    }


def enrich_sitemap_reports_for_site(
    reports: list[dict[str, str]],
    data: dict[str, Any],
    crawl_pages: list[dict[str, Any]],
) -> list[dict[str, str]]:
    existing = {report.get("name", "") for report in reports}
    candidate_urls = [
        str(data.get("finalDisplayedUrl") or ""),
        str(data.get("finalUrl") or ""),
        str(data.get("requestedUrl") or ""),
    ]
    candidate_urls.extend(str(page.get("url") or "") for page in crawl_pages[:5])
    candidate_keys = {site_key(url) for url in candidate_urls if url}
    if not candidate_keys or not REPORTS_DIR.exists():
        return reports
    enriched = list(reports)
    for path in sorted(REPORTS_DIR.glob("sitemap-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.name in existing:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        report_key = str(data.get("site_key") or "")
        report_site = str(data.get("site") or "")
        if report_key not in candidate_keys and site_key(report_site) not in candidate_keys:
            continue
        stat = path.stat()
        enriched.append({
            "name": path.name,
            "href": f"/reports/{path.name}",
            "kind": detect_kind(path.name),
            "size": format_size(stat.st_size),
            "updated": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        })
        existing.add(path.name)
    return enriched


def sitemap_file_kind(url: str) -> str:
    lower = url.lower()
    if "product" in lower:
        return "Products"
    if "collection" in lower or "category" in lower:
        return "Collections"
    if "blog" in lower or "article" in lower:
        return "Blog"
    if "page" in lower:
        return "Pages"
    if lower.endswith("/sitemap.xml"):
        return "Index"
    return "Sitemap"


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


def csv_cell(value: Any) -> Any:
    """Prevent spreadsheet applications from evaluating exported user text."""
    if not isinstance(value, str):
        return value
    return f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value


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
    if lowered.startswith("scan-manifest-") and lowered.endswith(".json"):
        return "Scan sampling manifest"
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

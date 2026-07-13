from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from celery import Celery

from content_quality import extract_content_issues, write_content_report
from crawler import crawl_site
from database import create_scan_job, get_scan_job, get_website, reconcile_issues, replace_crawl_pages, update_scan_job, websites_due_for_scan
from visual_evidence import attach_visual_evidence
from sampling import select_lighthouse_candidates
from budgets import evaluate_lighthouse_budgets


BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1")
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "/reports"))

celery_app = Celery("openaudit-worker", broker=BROKER_URL, backend=RESULT_BACKEND)
celery_app.conf.update(
    task_track_started=True,
    result_expires=86400,
    beat_schedule={
        "schedule-due-website-scans": {
            "task": "openaudit.schedule_due_scans",
            "schedule": float(os.getenv("SCHEDULE_CHECK_SECONDS", "300")),
        }
    },
)


@celery_app.task(name="openaudit.schedule_due_scans")
def schedule_due_scans() -> dict[str, Any]:
    queued = []
    for website in websites_due_for_scan():
        job = create_scan_job(str(website["key"]), "scheduled")
        result = run_lighthouse.delay(str(job["id"]))
        update_scan_job(str(job["id"]), task_id=str(result.id), message=f"Queued by {website['schedule']} schedule.")
        queued.append({"website_key": website["key"], "job_id": job["id"], "task_id": str(result.id)})
    return {"queued": queued, "count": len(queued)}


@celery_app.task(name="openaudit.run_lighthouse", bind=True)
def run_lighthouse(self: Any, job_id: str) -> dict[str, Any]:
    job = get_scan_job(job_id)
    if not job:
        raise ValueError(f"Scan job {job_id} was not found.")
    website = get_website(str(job["website_key"]))
    if not website:
        update_scan_job(job_id, status="failed", progress=100, message="Website not found.", finished_at=now())
        raise ValueError("Website not found.")

    scan_type = str(job.get("scan_type") or "full").lower()
    include_lighthouse = scan_type in {"lighthouse", "full", "scheduled"}
    include_pa11y = scan_type in {"accessibility", "full", "scheduled"}
    include_content = scan_type in {"content", "full", "scheduled"}
    if not include_lighthouse and not include_pa11y and not include_content:
        include_lighthouse = True
    update_scan_job(job_id, status="running", progress=5, message="Discovering sitemap and internal pages.", started_at=now(), task_id=self.request.id or "")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        pages = crawl_site(website, max_depth=int(os.getenv("CRAWL_MAX_DEPTH", "3")))
        replace_crawl_pages(str(website["key"]), pages)
        candidates = [page for page in pages if page.get("status_code", 0) < 400 and "html" in str(page.get("content_type") or "").lower()]
        candidates.sort(key=lambda page: (int(page.get("depth") or 0), 0 if page.get("source") == "homepage" else 1, str(page.get("url") or "")))
        candidates = candidates or [{"url": website["base_url"], "depth": 0, "source": "homepage"}]
        lighthouse_candidates = select_lighthouse_candidates(
            candidates, max(1, int(os.getenv("LIGHTHOUSE_PAGE_LIMIT", "10")))
        )
        pa11y_candidates = candidates[:max(1, int(os.getenv("PA11Y_PAGE_LIMIT", "20")))]
        update_scan_job(job_id, progress=20, message=f"Discovered {len(pages)} page(s). Starting audit tools.")

        all_issues: list[dict[str, Any]] = []
        report_paths: list[Path] = []
        tool_errors: list[str] = []
        scanned_sources: set[str] = set()
        if include_content:
            scanned_sources.add("content")
            content_issues = extract_content_issues(pages)
            all_issues.extend(content_issues)
            report_paths.append(write_content_report(REPORTS_DIR, str(website["key"]), pages, content_issues))
        if include_lighthouse:
            scanned_sources.add("lighthouse")
            scanned_sources.add("budget")
            concurrency = max(1, min(4, int(os.getenv("LIGHTHOUSE_CONCURRENCY", "2"))))
            with ThreadPoolExecutor(max_workers=min(concurrency, len(lighthouse_candidates))) as executor:
                futures = {
                    executor.submit(audit_lighthouse_candidate, str(website["key"]), page, index, website): (index, page)
                    for index, page in enumerate(lighthouse_candidates, start=1)
                }
                completed = 0
                for future in as_completed(futures):
                    index, page = futures[future]
                    completed += 1
                    progress = 20 + round((completed / len(lighthouse_candidates)) * (35 if include_pa11y else 60))
                    update_scan_job(job_id, progress=progress, message=f"Lighthouse {completed}/{len(lighthouse_candidates)} complete: {page['url']}")
                    try:
                        json_path, page_issues = future.result()
                    except Exception as exc:
                        tool_errors.append(f"Lighthouse {page['url']}: {exc}")
                        continue
                    report_paths.append(json_path)
                    all_issues.extend(page_issues)
        if include_pa11y:
            scanned_sources.add("pa11y")
            start_progress = 55 if include_lighthouse else 20
            span = 30 if include_lighthouse else 65
            for index, page in enumerate(pa11y_candidates, start=1):
                progress = start_progress + round((index / len(pa11y_candidates)) * span)
                update_scan_job(job_id, progress=progress, message=f"Pa11y {index}/{len(pa11y_candidates)}: {page['url']}")
                try:
                    pa11y_path, pa11y_issues = run_pa11y_page(str(website["key"]), str(page["url"]), index)
                except Exception as exc:
                    tool_errors.append(f"Pa11y {page['url']}: {exc}")
                    continue
                report_paths.append(pa11y_path)
                attach_visual_evidence(REPORTS_DIR, str(website["key"]), str(page["url"]), f"pa11y-{index}", pa11y_issues)
                all_issues.extend(pa11y_issues)
        if not report_paths:
            raise RuntimeError("No audit page completed successfully. " + " | ".join(tool_errors[:3]))
        report_paths.append(write_scan_manifest(
            str(website["key"]), pages, lighthouse_candidates if include_lighthouse else [],
            pa11y_candidates if include_pa11y else [],
        ))
    except Exception as exc:
        update_scan_job(job_id, status="failed", progress=100, message=str(exc), finished_at=now())
        raise

    update_scan_job(job_id, progress=85, message="Merging page findings and reconciling issue lifecycle.")
    issues = merge_issues(all_issues)
    report_names = [path.name for path in report_paths]
    changes = reconcile_issues(str(website["key"]), issues, ",".join(report_names), scanned_sources=scanned_sources)
    tools = " + ".join(source.title() for source in sorted(scanned_sources))
    warning = f" {len(tool_errors)} page audit(s) failed." if tool_errors else ""
    message = f"Completed {len(pages)} crawled page(s) with {tools}. {changes['current']} current, {changes['opened']} new, {changes['resolved']} resolved.{warning}"
    update_scan_job(job_id, status="completed", progress=100, message=message, report_path=",".join(report_names), finished_at=now())
    return {"job_id": job_id, "reports": report_names, "crawled_pages": len(pages), "issues": changes}


def run_lighthouse_page(website_key: str, url: str, index: int) -> Path:
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(":", "-").replace(".", "-")
    base_path = REPORTS_DIR / f"lighthouse-{website_key}-{index}-{stamp}.report"
    command = [
        "npx", "lighthouse", url, "--output=html", "--output=json", f"--output-path={base_path}",
        "--chrome-flags=--headless=new --no-sandbox --disable-dev-shm-usage",
    ]
    if os.getenv("LIGHTHOUSE_SETTINGS_PRESET", "desktop").lower() == "desktop":
        command.append("--preset=desktop")
    result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Lighthouse failed.")[-3000:])
    json_path = Path(f"{base_path}.json")
    return json_path if json_path.exists() else Path(f"{base_path}.report.json")


def audit_lighthouse_candidate(website_key: str, page: dict[str, Any], index: int,
                               website: dict[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
    url = str(page["url"])
    json_path = run_lighthouse_page(website_key, url, index)
    issues: list[dict[str, Any]] = []
    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        issues = extract_issues(data.get("categories", {}), data.get("audits", {}), url)
        issues.extend(evaluate_lighthouse_budgets(data, url, website))
        attach_visual_evidence(REPORTS_DIR, website_key, url, f"lighthouse-{index}", issues)
    return json_path, issues


def write_scan_manifest(website_key: str, pages: list[dict[str, Any]], lighthouse_pages: list[dict[str, Any]], pa11y_pages: list[dict[str, Any]]) -> Path:
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(":", "-").replace(".", "-")
    path = REPORTS_DIR / f"scan-manifest-{website_key}-{stamp}.json"
    payload = {
        "website_key": website_key,
        "generated_at": now().isoformat(),
        "discovered_pages": len(pages),
        "lighthouse": [
            {"url": str(page.get("url") or ""), "route_group": str(page.get("sample_group") or "")}
            for page in lighthouse_pages
        ],
        "pa11y": [str(page.get("url") or "") for page in pa11y_pages],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def run_pa11y_page(website_key: str, url: str, index: int) -> tuple[Path, list[dict[str, Any]]]:
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(":", "-").replace(".", "-")
    output_path = REPORTS_DIR / f"pa11y-{website_key}-{index}-{stamp}.json"
    result = subprocess.run(["node", "/app/pa11y-runner.js", url], capture_output=True, text=True, timeout=180, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Pa11y failed.")[-3000:])
    data = json.loads(result.stdout or "{}")
    output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return output_path, extract_pa11y_issues(data, url)


def extract_issues(categories: dict[str, Any], audits: dict[str, Any], page_url: str = "") -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    labels = {"performance": "Performance", "accessibility": "Accessibility", "best-practices": "Best Practices", "seo": "SEO", "pwa": "PWA"}
    for category_id, category in categories.items():
        label = labels.get(category_id, str(category.get("title") or category_id))
        for ref in category.get("auditRefs", []):
            audit_id = str(ref.get("id") or "")
            audit = audits.get(audit_id) or {}
            score = audit.get("score")
            weight = float(ref.get("weight") or 0)
            if not isinstance(score, (int, float)) or score >= 0.9 or weight <= 0 or audit.get("scoreDisplayMode") in {"notApplicable", "manual", "informative"}:
                continue
            details = audit.get("details") or {}
            occurrences = len(details.get("items") or []) or 1
            points = max(0.1, (1 - score) * weight * 2.5)
            examples = []
            for item in (details.get("items") or [])[:20]:
                node = item.get("node") if isinstance(item, dict) else {}
                node = node if isinstance(node, dict) else {}
                examples.append({
                    "page_url": page_url,
                    "selector": str(node.get("selector") or item.get("selector") or ""),
                    "snippet": str(node.get("snippet") or item.get("snippet") or ""),
                    "explanation": str(node.get("explanation") or item.get("explanation") or ""),
                })
            rows[audit_id] = {
                "id": audit_id, "title": str(audit.get("title") or audit_id), "category": label,
                "source": "lighthouse",
                "difficulty": "High" if points >= 3 else "Medium" if points >= 1 else "Low",
                "responsibility": owner_for(label), "occurrences": occurrences, "points": round(points, 1),
                "page_url": page_url, "affected_examples": examples,
            }
    return sorted(rows.values(), key=lambda item: float(item["points"]), reverse=True)


def extract_pa11y_issues(data: dict[str, Any], page_url: str) -> list[dict[str, Any]]:
    rows = []
    for item in data.get("issues", []):
        severity = str(item.get("type") or "error").lower()
        points = 3.0 if severity == "error" else 1.0 if severity == "warning" else 0.3
        code = str(item.get("code") or item.get("message") or "pa11y-issue")
        rows.append({
            "id": code,
            "source": "pa11y",
            "title": str(item.get("message") or code),
            "category": "Accessibility",
            "difficulty": "High" if severity == "error" else "Medium" if severity == "warning" else "Low",
            "responsibility": "Accessibility / Development",
            "occurrences": 1,
            "points": points,
            "page_url": page_url,
            "affected_examples": [{
                "page_url": page_url,
                "selector": str(item.get("selector") or ""),
                "snippet": str(item.get("context") or ""),
                "explanation": str(item.get("message") or ""),
            }],
        })
    return rows


def merge_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for issue in issues:
        key = f"{issue.get('source', 'lighthouse')}:{issue.get('id') or issue.get('title') or ''}"
        if key not in merged:
            merged[key] = {**issue, "occurrences": 0, "affected_examples": [], "pages": set()}
        row = merged[key]
        row["occurrences"] += int(issue.get("occurrences") or 0)
        row["points"] = max(float(row.get("points") or 0), float(issue.get("points") or 0))
        row["affected_examples"].extend(issue.get("affected_examples") or [])
        if issue.get("page_url"):
            row["pages"].add(str(issue["page_url"]))
    result = []
    for row in merged.values():
        pages = row.pop("pages")
        row["page_count"] = len(pages)
        row["affected_examples"] = row["affected_examples"][:40]
        result.append(row)
    return sorted(result, key=lambda item: float(item.get("points") or 0), reverse=True)


def owner_for(category: str) -> str:
    if category == "SEO":
        return "SEO / Content"
    if category == "Accessibility":
        return "Accessibility / Development"
    return "Development"


def now() -> datetime:
    return datetime.now(timezone.utc)

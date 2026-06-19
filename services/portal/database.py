from __future__ import annotations

import os
import ipaddress
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Website(Base):
    __tablename__ = "websites"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schedule: Mapped[str] = mapped_column(String(80), default="manual", nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    exclude_paths: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ScanJob(Base):
    __tablename__ = "scan_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    website_key: Mapped[str] = mapped_column(ForeignKey("websites.key", ondelete="CASCADE"), index=True)
    scan_type: Mapped[str] = mapped_column(String(80), default="full")
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    task_id: Mapped[str] = mapped_column(String(255), default="")
    report_path: Mapped[str] = mapped_column(String(2048), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditIssue(Base):
    __tablename__ = "audit_issues"
    __table_args__ = (UniqueConstraint("website_key", "issue_key", name="uq_issue_website_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    website_key: Mapped[str] = mapped_column(ForeignKey("websites.key", ondelete="CASCADE"), index=True)
    issue_key: Mapped[str] = mapped_column(String(255), index=True)
    audit_id: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(40), default="lighthouse", index=True)
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(120), default="General")
    status: Mapped[str] = mapped_column(String(40), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(40), default="medium")
    owner: Mapped[str] = mapped_column(String(255), default="Unassigned")
    occurrences: Mapped[int] = mapped_column(Integer, default=0)
    affected_pages: Mapped[int] = mapped_column(Integer, default=1)
    points: Mapped[float] = mapped_column(Float, default=0)
    source_report: Mapped[str] = mapped_column(String(2048), default="")
    ignored_reason: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CrawlPage(Base):
    __tablename__ = "crawl_pages"
    __table_args__ = (UniqueConstraint("website_key", "url", name="uq_crawl_page_website_url"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    website_key: Mapped[str] = mapped_column(ForeignKey("websites.key", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(Text, default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(80), default="internal-link")
    content_type: Mapped[str] = mapped_column(String(255), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    last_crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IssueEvidence(Base):
    __tablename__ = "issue_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    audit_issue_id: Mapped[str] = mapped_column(ForeignKey("audit_issues.id", ondelete="CASCADE"), index=True)
    page_url: Mapped[str] = mapped_column(String(2048), default="")
    selector: Mapped[str] = mapped_column(Text, default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///openaudit.db")
if _DATABASE_URL.startswith("sqlite:///") and not _DATABASE_URL.startswith("sqlite:////"):
    sqlite_path = _DATABASE_URL.removeprefix("sqlite:///")
    if sqlite_path and sqlite_path != ":memory:":
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
_engine = create_engine(_DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def init_database(urls_file: Path | None = None) -> None:
    Base.metadata.create_all(_engine)
    if urls_file and urls_file.exists():
        for line in urls_file.read_text(encoding="utf-8").splitlines():
            url = line.strip()
            if url and not url.startswith("#"):
                ensure_website(url, source_name="Imported website")


def normalized_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Website URL is required.")
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a valid HTTP or HTTPS website URL.")
    if parsed.username or parsed.password:
        raise ValueError("Website URLs must not contain embedded credentials.")
    clean_url = value.rstrip("/") + "/"
    assert_safe_target_url(clean_url, resolve_dns=False)
    return clean_url


def assert_safe_target_url(value: str, resolve_dns: bool = True) -> None:
    if os.getenv("ALLOW_PRIVATE_TARGETS", "false").lower() in {"1", "true", "yes"}:
        return
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise ValueError("Target URL has no hostname.")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local and internal hostnames are blocked by the scan safety policy.")
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(hostname)))
    except ValueError:
        if resolve_dns:
            try:
                addresses.update(info[4][0] for info in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM))
            except socket.gaierror as exc:
                raise ValueError(f"Target hostname could not be resolved: {hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError(f"Private or reserved target address is blocked: {address}")


def website_key(value: str) -> str:
    host = urlparse(normalized_url(value)).netloc.lower().split(":", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "website"


def website_name(value: str) -> str:
    host = urlparse(normalized_url(value)).netloc.lower().split(":", 1)[0]
    return host.removeprefix("www.")


def ensure_website(url: str, source_name: str = "") -> dict[str, Any]:
    clean_url = normalized_url(url)
    key = website_key(clean_url)
    with SessionLocal.begin() as session:
        website = session.get(Website, key)
        if website is None:
            website = Website(
                key=key,
                name=source_name if source_name and source_name != "Imported website" else website_name(clean_url),
                base_url=clean_url,
            )
            session.add(website)
        return website_dict(website)


def list_websites(active_only: bool = False) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        statement = select(Website)
        if active_only:
            statement = statement.where(Website.active.is_(True))
        statement = statement.order_by(Website.name.asc())
        return [website_dict(row) for row in session.scalars(statement)]


def get_website(key: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        website = session.get(Website, key)
        return website_dict(website) if website else None


def create_website(payload: dict[str, Any]) -> dict[str, Any]:
    clean_url = normalized_url(str(payload.get("base_url") or payload.get("url") or ""))
    key = website_key(clean_url)
    with SessionLocal.begin() as session:
        if session.get(Website, key):
            raise ValueError("This website already exists.")
        website = Website(
            key=key,
            name=str(payload.get("name") or website_name(clean_url)).strip(),
            base_url=clean_url,
            active=bool(payload.get("active", True)),
            schedule=str(payload.get("schedule") or "manual"),
            max_pages=max(1, min(int(payload.get("max_pages") or 100), 10000)),
            exclude_paths=str(payload.get("exclude_paths") or "").strip(),
        )
        session.add(website)
        return website_dict(website)


def update_website(key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    with SessionLocal.begin() as session:
        website = session.get(Website, key)
        if website is None:
            return None
        if "name" in payload:
            website.name = str(payload["name"]).strip() or website.name
        if "active" in payload:
            website.active = bool(payload["active"])
        if "schedule" in payload:
            website.schedule = str(payload["schedule"] or "manual")
        if "max_pages" in payload:
            website.max_pages = max(1, min(int(payload["max_pages"]), 10000))
        if "exclude_paths" in payload:
            website.exclude_paths = str(payload["exclude_paths"] or "").strip()
        website.updated_at = utcnow()
        return website_dict(website)


def delete_website(key: str) -> bool:
    with SessionLocal.begin() as session:
        website = session.get(Website, key)
        if website is None:
            return False
        session.delete(website)
        return True


def create_scan_job(website_key_value: str, scan_type: str = "full") -> dict[str, Any]:
    with SessionLocal.begin() as session:
        if session.get(Website, website_key_value) is None:
            raise ValueError("Website not found.")
        job = ScanJob(website_key=website_key_value, scan_type=scan_type)
        session.add(job)
        session.flush()
        return scan_dict(job)


def update_scan_job(job_id: str, **changes: Any) -> dict[str, Any] | None:
    allowed = {"status", "progress", "message", "task_id", "report_path", "started_at", "finished_at"}
    with SessionLocal.begin() as session:
        job = session.get(ScanJob, job_id)
        if job is None:
            return None
        for key, value in changes.items():
            if key in allowed:
                setattr(job, key, value)
        return scan_dict(job)


def get_scan_job(job_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        job = session.get(ScanJob, job_id)
        return scan_dict(job) if job else None


def list_scan_jobs(website_key_value: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        statement = select(ScanJob)
        if website_key_value:
            statement = statement.where(ScanJob.website_key == website_key_value)
        statement = statement.order_by(ScanJob.created_at.desc()).limit(limit)
        return [scan_dict(row) for row in session.scalars(statement)]


def websites_due_for_scan() -> list[dict[str, Any]]:
    intervals = {"daily": timedelta(days=1), "weekly": timedelta(days=7), "monthly": timedelta(days=30)}
    now = utcnow()
    due: list[dict[str, Any]] = []
    with SessionLocal() as session:
        websites = session.scalars(select(Website).where(Website.active.is_(True), Website.schedule != "manual"))
        for website in websites:
            interval = intervals.get(website.schedule)
            if not interval:
                continue
            active_job = session.scalar(
                select(ScanJob).where(ScanJob.website_key == website.key, ScanJob.status.in_(["queued", "running"]))
            )
            if active_job:
                continue
            latest = session.scalar(
                select(ScanJob).where(ScanJob.website_key == website.key).order_by(ScanJob.created_at.desc()).limit(1)
            )
            last_time = latest.finished_at or latest.created_at if latest else None
            if last_time is None:
                due.append(website_dict(website))
                continue
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            if now - last_time >= interval:
                due.append(website_dict(website))
    return due


def replace_crawl_pages(website_key_value: str, pages: list[dict[str, Any]]) -> None:
    with SessionLocal.begin() as session:
        existing = {
            row.url: row
            for row in session.scalars(select(CrawlPage).where(CrawlPage.website_key == website_key_value))
        }
        for page in pages:
            url = str(page.get("url") or "")
            if not url:
                continue
            row = existing.get(url)
            if row is None:
                row = CrawlPage(website_key=website_key_value, url=url)
                session.add(row)
            row.title = str(page.get("title") or "")
            row.status_code = int(page.get("status_code") or 0)
            row.depth = int(page.get("depth") or 0)
            row.source = str(page.get("source") or "internal-link")
            row.content_type = str(page.get("content_type") or "")
            row.error = str(page.get("error") or "")
            row.last_crawled_at = utcnow()


def list_crawl_pages(website_key_value: str, limit: int = 500) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        statement = (
            select(CrawlPage)
            .where(CrawlPage.website_key == website_key_value)
            .order_by(CrawlPage.depth.asc(), CrawlPage.url.asc())
            .limit(limit)
        )
        return [crawl_page_dict(row) for row in session.scalars(statement)]


def reconcile_issues(
    website_key_value: str,
    issues: list[dict[str, Any]],
    report: str = "",
    scanned_sources: set[str] | None = None,
) -> dict[str, int]:
    now = utcnow()
    current_keys: set[str] = set()
    opened = reopened = resolved = 0
    with SessionLocal.begin() as session:
        existing = {
            row.issue_key: row
            for row in session.scalars(select(AuditIssue).where(AuditIssue.website_key == website_key_value))
        }
        for issue in issues:
            raw_key = str(issue.get("id") or issue.get("title") or "").strip().lower()
            source = str(issue.get("source") or "lighthouse").strip().lower()
            key = raw_key if source == "lighthouse" else f"{source}:{raw_key}"
            if not raw_key:
                continue
            current_keys.add(key)
            row = existing.get(key)
            if row is None:
                row = AuditIssue(website_key=website_key_value, issue_key=key, audit_id=str(issue.get("id") or ""), title=str(issue.get("title") or key))
                session.add(row)
                existing[key] = row
                opened += 1
            elif row.status == "resolved":
                row.status = "reopened"
                row.resolved_at = None
                reopened += 1
            if row.status not in {"open", "assigned", "in_progress", "ignored", "reopened"}:
                row.status = "open"
            row.category = str(issue.get("category") or "General")
            row.source = source
            row.priority = str(issue.get("difficulty") or "medium").lower()
            row.owner = str(issue.get("responsibility") or issue.get("role") or "Unassigned")
            row.occurrences = int(float(issue.get("occurrences") or 0))
            row.affected_pages = max(1, int(issue.get("page_count") or issue.get("pages") or 1))
            row.points = float(issue.get("points") or 0)
            row.source_report = report
            row.last_seen_at = now
            session.flush()
            session.query(IssueEvidence).filter(IssueEvidence.audit_issue_id == row.id).delete()
            for example in (issue.get("affected_examples") or [])[:20]:
                session.add(
                    IssueEvidence(
                        audit_issue_id=row.id,
                        page_url=str(example.get("page_url") or issue.get("page_url") or ""),
                        selector=str(example.get("selector") or ""),
                        snippet=str(example.get("snippet") or ""),
                        explanation=str(example.get("explanation") or ""),
                    )
                )

        resolved_sources = scanned_sources or {str(issue.get("source") or "lighthouse").lower() for issue in issues} or {"lighthouse"}
        for key, row in existing.items():
            if row.source in resolved_sources and key not in current_keys and row.status not in {"resolved", "ignored"}:
                row.status = "resolved"
                row.resolved_at = now
                resolved += 1
    return {"opened": opened, "reopened": reopened, "resolved": resolved, "current": len(current_keys)}


def list_issues(website_key_value: str, status: str = "") -> list[dict[str, Any]]:
    with SessionLocal() as session:
        statement = select(AuditIssue).where(AuditIssue.website_key == website_key_value)
        if status:
            statement = statement.where(AuditIssue.status == status)
        statement = statement.order_by(AuditIssue.points.desc(), AuditIssue.updated_at.desc())
        rows = []
        for row in session.scalars(statement):
            evidence = session.scalars(
                select(IssueEvidence)
                .where(IssueEvidence.audit_issue_id == row.id)
                .order_by(IssueEvidence.captured_at.desc())
                .limit(20)
            )
            item = issue_dict(row)
            item["evidence"] = [evidence_dict(entry) for entry in evidence]
            rows.append(item)
        return rows


def update_issue_status(issue_id: str, status: str, owner: str = "", ignored_reason: str = "") -> dict[str, Any] | None:
    allowed = {"open", "assigned", "in_progress", "resolved", "ignored", "reopened"}
    if status not in allowed:
        raise ValueError("Invalid issue status.")
    with SessionLocal.begin() as session:
        issue = session.get(AuditIssue, issue_id)
        if issue is None:
            return None
        issue.status = status
        if owner:
            issue.owner = owner
        issue.ignored_reason = ignored_reason if status == "ignored" else ""
        issue.resolved_at = utcnow() if status == "resolved" else None
        issue.updated_at = utcnow()
        return issue_dict(issue)


def website_dict(row: Website) -> dict[str, Any]:
    return {
        "key": row.key, "name": row.name, "label": row.name, "url": row.base_url,
        "base_url": row.base_url, "active": row.active, "schedule": row.schedule,
        "max_pages": row.max_pages, "exclude_paths": row.exclude_paths,
        "created_at": iso(row.created_at), "updated_at": iso(row.updated_at), "source": "database",
    }


def scan_dict(row: ScanJob) -> dict[str, Any]:
    return {
        "id": row.id, "website_key": row.website_key, "scan_type": row.scan_type,
        "status": row.status, "progress": row.progress, "message": row.message,
        "task_id": row.task_id, "report_path": row.report_path,
        "created_at": iso(row.created_at), "started_at": iso(row.started_at), "finished_at": iso(row.finished_at),
    }


def issue_dict(row: AuditIssue) -> dict[str, Any]:
    return {
        "id": row.id, "website_key": row.website_key, "issue_key": row.issue_key,
        "audit_id": row.audit_id, "source": row.source, "title": row.title, "category": row.category,
        "status": row.status, "priority": row.priority, "owner": row.owner,
        "occurrences": row.occurrences, "affected_pages": row.affected_pages, "points": row.points, "source_report": row.source_report,
        "ignored_reason": row.ignored_reason, "first_seen_at": iso(row.first_seen_at),
        "last_seen_at": iso(row.last_seen_at), "resolved_at": iso(row.resolved_at), "updated_at": iso(row.updated_at),
    }


def crawl_page_dict(row: CrawlPage) -> dict[str, Any]:
    return {
        "id": row.id, "website_key": row.website_key, "url": row.url, "title": row.title,
        "status_code": row.status_code, "depth": row.depth, "source": row.source,
        "content_type": row.content_type, "error": row.error, "last_crawled_at": iso(row.last_crawled_at),
    }


def evidence_dict(row: IssueEvidence) -> dict[str, Any]:
    return {
        "id": row.id, "page_url": row.page_url, "selector": row.selector,
        "snippet": row.snippet, "explanation": row.explanation, "captured_at": iso(row.captured_at),
    }


def iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""

"""Initial OpenAudit operational schema."""

from alembic import op
import sqlalchemy as sa


revision = "20260618_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "websites" not in existing:
        op.create_table(
        "websites",
        sa.Column("key", sa.String(160), primary_key=True), sa.Column("name", sa.String(255), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False, unique=True), sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("schedule", sa.String(80), nullable=False), sa.Column("max_pages", sa.Integer(), nullable=False),
        sa.Column("exclude_paths", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
    if "scan_jobs" not in existing:
        op.create_table(
        "scan_jobs",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("website_key", sa.String(160), sa.ForeignKey("websites.key", ondelete="CASCADE"), nullable=False),
        sa.Column("scan_type", sa.String(80), nullable=False), sa.Column("status", sa.String(40), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False), sa.Column("message", sa.Text(), nullable=False),
        sa.Column("task_id", sa.String(255), nullable=False), sa.Column("report_path", sa.String(2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_scan_jobs_website_key", "scan_jobs", ["website_key"])
        op.create_index("ix_scan_jobs_status", "scan_jobs", ["status"])
    if "audit_issues" not in existing:
        op.create_table(
        "audit_issues",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("website_key", sa.String(160), sa.ForeignKey("websites.key", ondelete="CASCADE"), nullable=False),
        sa.Column("issue_key", sa.String(255), nullable=False), sa.Column("audit_id", sa.String(255), nullable=False), sa.Column("source", sa.String(40), nullable=False),
        sa.Column("title", sa.Text(), nullable=False), sa.Column("category", sa.String(120), nullable=False),
        sa.Column("status", sa.String(40), nullable=False), sa.Column("priority", sa.String(40), nullable=False),
        sa.Column("owner", sa.String(255), nullable=False), sa.Column("occurrences", sa.Integer(), nullable=False), sa.Column("affected_pages", sa.Integer(), nullable=False),
        sa.Column("points", sa.Float(), nullable=False), sa.Column("source_report", sa.String(2048), nullable=False),
        sa.Column("ignored_reason", sa.Text(), nullable=False), sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)), sa.Column("resolved_at", sa.DateTime(timezone=True)), sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("website_key", "issue_key", name="uq_issue_website_key"),
        )
        op.create_index("ix_audit_issues_website_key", "audit_issues", ["website_key"])
        op.create_index("ix_audit_issues_issue_key", "audit_issues", ["issue_key"])
        op.create_index("ix_audit_issues_status", "audit_issues", ["status"])
        op.create_index("ix_audit_issues_source", "audit_issues", ["source"])
    else:
        issue_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("audit_issues")}
        if "source" not in issue_columns:
            op.add_column("audit_issues", sa.Column("source", sa.String(40), nullable=False, server_default="lighthouse"))
            op.create_index("ix_audit_issues_source", "audit_issues", ["source"])
        if "affected_pages" not in issue_columns:
            op.add_column("audit_issues", sa.Column("affected_pages", sa.Integer(), nullable=False, server_default="1"))
    if "crawl_pages" not in existing:
        op.create_table(
        "crawl_pages",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("website_key", sa.String(160), sa.ForeignKey("websites.key", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False), sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False), sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(80), nullable=False), sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("error", sa.Text(), nullable=False), sa.Column("last_crawled_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("website_key", "url", name="uq_crawl_page_website_url"),
        )
        op.create_index("ix_crawl_pages_website_key", "crawl_pages", ["website_key"])
    if "issue_evidence" not in existing:
        op.create_table(
        "issue_evidence",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("audit_issue_id", sa.String(36), sa.ForeignKey("audit_issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_url", sa.String(2048), nullable=False), sa.Column("selector", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False), sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_issue_evidence_audit_issue_id", "issue_evidence", ["audit_issue_id"])


def downgrade() -> None:
    op.drop_table("issue_evidence")
    op.drop_table("crawl_pages")
    op.drop_table("audit_issues")
    op.drop_table("scan_jobs")
    op.drop_table("websites")

"""Add scanner source and page evidence compatibility fields.

Revision ID: 20260618_0002
Revises: 20260618_0001
"""

from alembic import op
import sqlalchemy as sa


revision = "20260618_0002"
down_revision = "20260618_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "audit_issues" in tables:
        columns = {column["name"] for column in inspector.get_columns("audit_issues")}
        indexes = {index["name"] for index in inspector.get_indexes("audit_issues")}
        if "source" not in columns:
            op.add_column(
                "audit_issues",
                sa.Column("source", sa.String(40), nullable=False, server_default="lighthouse"),
            )
        if "ix_audit_issues_source" not in indexes:
            op.create_index("ix_audit_issues_source", "audit_issues", ["source"])
        if "affected_pages" not in columns:
            op.add_column(
                "audit_issues",
                sa.Column("affected_pages", sa.Integer(), nullable=False, server_default="1"),
            )

    if "crawl_pages" not in tables:
        op.create_table(
            "crawl_pages",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("website_key", sa.String(160), sa.ForeignKey("websites.key", ondelete="CASCADE"), nullable=False),
            sa.Column("url", sa.String(2048), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=False),
            sa.Column("depth", sa.Integer(), nullable=False),
            sa.Column("source", sa.String(80), nullable=False),
            sa.Column("content_type", sa.String(255), nullable=False),
            sa.Column("error", sa.Text(), nullable=False),
            sa.Column("last_crawled_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("website_key", "url", name="uq_crawl_page_website_url"),
        )
        op.create_index("ix_crawl_pages_website_key", "crawl_pages", ["website_key"])

    if "issue_evidence" not in tables:
        op.create_table(
            "issue_evidence",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("audit_issue_id", sa.String(36), sa.ForeignKey("audit_issues.id", ondelete="CASCADE"), nullable=False),
            sa.Column("page_url", sa.String(2048), nullable=False),
            sa.Column("selector", sa.Text(), nullable=False),
            sa.Column("snippet", sa.Text(), nullable=False),
            sa.Column("explanation", sa.Text(), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_issue_evidence_audit_issue_id", "issue_evidence", ["audit_issue_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "audit_issues" in tables:
        columns = {column["name"] for column in inspector.get_columns("audit_issues")}
        indexes = {index["name"] for index in inspector.get_indexes("audit_issues")}
        if "ix_audit_issues_source" in indexes:
            op.drop_index("ix_audit_issues_source", table_name="audit_issues")
        if "source" in columns:
            op.drop_column("audit_issues", "source")
        if "affected_pages" in columns:
            op.drop_column("audit_issues", "affected_pages")

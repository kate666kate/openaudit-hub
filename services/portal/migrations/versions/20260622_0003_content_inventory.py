"""Add content inventory fields to crawled pages.

Revision ID: 20260622_0003
Revises: 20260618_0002
"""

from alembic import op
import sqlalchemy as sa


revision = "20260622_0003"
down_revision = "20260618_0002"
branch_labels = None
depends_on = None


FIELDS = [
    ("meta_description", sa.Text(), ""),
    ("language", sa.String(40), ""),
    ("word_count", sa.Integer(), "0"),
    ("h1_count", sa.Integer(), "0"),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "crawl_pages" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("crawl_pages")}
    for name, column_type, default in FIELDS:
        if name not in columns:
            op.add_column(
                "crawl_pages",
                sa.Column(name, column_type, nullable=False, server_default=default),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "crawl_pages" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("crawl_pages")}
    for name, _, _ in reversed(FIELDS):
        if name in columns:
            op.drop_column("crawl_pages", name)

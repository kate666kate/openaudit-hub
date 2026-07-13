"""Add privacy-preserving content fingerprints.

Revision ID: 20260701_0005
Revises: 20260701_0004
"""

from alembic import op
import sqlalchemy as sa


revision = "20260701_0005"
down_revision = "20260701_0004"
branch_labels = None
depends_on = None


FIELDS = [
    ("content_hash", sa.String(64)),
    ("content_simhash", sa.String(16)),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "crawl_pages" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("crawl_pages")}
    for name, column_type in FIELDS:
        if name not in columns:
            op.add_column(
                "crawl_pages",
                sa.Column(name, column_type, nullable=False, server_default=""),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "crawl_pages" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("crawl_pages")}
    for name, _ in reversed(FIELDS):
        if name in columns:
            op.drop_column("crawl_pages", name)

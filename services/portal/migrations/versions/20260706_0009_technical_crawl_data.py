"""Add extensible technical crawl data."""

from alembic import op
import sqlalchemy as sa

revision = "20260706_0009"
down_revision = "20260706_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("crawl_pages")}
    if "technical_data" not in columns:
        op.add_column("crawl_pages", sa.Column("technical_data", sa.Text(), nullable=False, server_default="{}"))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("crawl_pages")}
    if "technical_data" in columns:
        op.drop_column("crawl_pages", "technical_data")

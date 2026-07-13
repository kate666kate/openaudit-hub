"""Add website performance budgets."""

from alembic import op
import sqlalchemy as sa

revision = "20260706_0008"
down_revision = "20260702_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("websites")}
    definitions = (
        ("budget_performance", sa.Integer(), 70),
        ("budget_accessibility", sa.Integer(), 80),
        ("budget_seo", sa.Integer(), 80),
        ("budget_lcp_ms", sa.Integer(), 2500),
        ("budget_cls", sa.Float(), 0.1),
    )
    for name, column_type, default in definitions:
        if name not in columns:
            op.add_column("websites", sa.Column(name, column_type, nullable=False, server_default=str(default)))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("websites")}
    for name in ("budget_cls", "budget_lcp_ms", "budget_seo", "budget_accessibility", "budget_performance"):
        if name in columns:
            op.drop_column("websites", name)

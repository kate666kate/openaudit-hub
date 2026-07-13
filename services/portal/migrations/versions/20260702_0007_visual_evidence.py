"""Add visual evidence fields to issue findings."""

from alembic import op
import sqlalchemy as sa

revision = "20260702_0007"
down_revision = "20260701_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("issue_evidence")}
    if "screenshot_path" not in columns:
        op.add_column("issue_evidence", sa.Column("screenshot_path", sa.String(2048), nullable=False, server_default=""))
    if "highlight" not in columns:
        op.add_column("issue_evidence", sa.Column("highlight", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("issue_evidence")}
    if "highlight" in columns:
        op.drop_column("issue_evidence", "highlight")
    if "screenshot_path" in columns:
        op.drop_column("issue_evidence", "screenshot_path")

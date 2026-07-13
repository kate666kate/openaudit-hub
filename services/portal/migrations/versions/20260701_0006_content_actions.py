"""Add content governance activity actions.

Revision ID: 20260701_0006
Revises: 20260701_0005
"""

from alembic import op
import sqlalchemy as sa

revision = "20260701_0006"
down_revision = "20260701_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "content_actions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "content_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("website_key", sa.String(160), nullable=False),
        sa.Column("action_key", sa.String(64), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False, server_default="duplicate-content"),
        sa.Column("title", sa.Text(), nullable=False, server_default="Content action"),
        sa.Column("primary_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("affected_urls", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(40), nullable=False, server_default="suggested"),
        sa.Column("owner", sa.String(255), nullable=False, server_default="Unassigned"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("points", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["website_key"], ["websites.key"], ondelete="CASCADE"),
        sa.UniqueConstraint("website_key", "action_key", name="uq_content_action_website_key"),
    )
    op.create_index("ix_content_actions_website_key", "content_actions", ["website_key"])
    op.create_index("ix_content_actions_action_key", "content_actions", ["action_key"])
    op.create_index("ix_content_actions_status", "content_actions", ["status"])


def downgrade() -> None:
    if "content_actions" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("content_actions")

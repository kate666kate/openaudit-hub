"""Add persistent keyword recommendation workflow.

Revision ID: 20260701_0004
Revises: 20260622_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "20260701_0004"
down_revision = "20260622_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "keyword_actions" in inspector.get_table_names():
        return
    op.create_table(
        "keyword_actions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("website_key", sa.String(length=160), nullable=False),
        sa.Column("action_key", sa.String(length=64), nullable=False),
        sa.Column("page_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("keyword", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("decision", sa.String(length=160), nullable=False, server_default="Improve existing page"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="suggested"),
        sa.Column("owner", sa.String(length=255), nullable=False, server_default="Unassigned"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["website_key"], ["websites.key"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("website_key", "action_key", name="uq_keyword_action_website_key"),
    )
    op.create_index("ix_keyword_actions_website_key", "keyword_actions", ["website_key"])
    op.create_index("ix_keyword_actions_action_key", "keyword_actions", ["action_key"])
    op.create_index("ix_keyword_actions_status", "keyword_actions", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "keyword_actions" not in inspector.get_table_names():
        return
    op.drop_index("ix_keyword_actions_status", table_name="keyword_actions")
    op.drop_index("ix_keyword_actions_action_key", table_name="keyword_actions")
    op.drop_index("ix_keyword_actions_website_key", table_name="keyword_actions")
    op.drop_table("keyword_actions")

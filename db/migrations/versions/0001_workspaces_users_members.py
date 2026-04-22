"""workspaces, users, workspace_members + RLS

Revision ID: 0001
Revises:
Create Date: 2026-04-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Enums ──────────────────────────────────────────────────────────────
    member_role = postgresql.ENUM("admin", "analyst", "viewer", name="member_role")
    member_role.create(op.get_bind(), checkfirst=True)

    # ── Tables ─────────────────────────────────────────────────────────────
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("clerk_user_id", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "workspace_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Enum("admin", "analyst", "viewer", name="member_role"), nullable=False),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
    )

    # ── RLS on workspace_members ───────────────────────────────────────────
    op.execute("ALTER TABLE workspace_members ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY workspace_members_isolation ON workspace_members
            USING (
                workspace_id = current_setting('app.current_workspace_id', true)::uuid
            )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS workspace_members_isolation ON workspace_members")
    op.execute("ALTER TABLE workspace_members DISABLE ROW LEVEL SECURITY")
    op.drop_table("workspace_members")
    op.drop_table("users")
    op.drop_table("workspaces")
    op.execute("DROP TYPE IF EXISTS member_role")

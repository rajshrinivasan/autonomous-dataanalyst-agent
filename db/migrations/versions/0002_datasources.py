"""datasources table + RLS

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    datasource_type = postgresql.ENUM(
        "sqlite", "postgres", "bigquery", "snowflake",
        name="datasource_type",
    )
    datasource_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "datasources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM("sqlite", "postgres", "bigquery", "snowflake", name="datasource_type", create_type=False),
            nullable=False,
        ),
        sa.Column("connection_secret_ref", sa.String(255), nullable=False),
        sa.Column("default_schema", sa.String(255), nullable=True),
        sa.Column("row_limit", sa.Integer(), nullable=False, server_default="50"),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ── RLS on datasources ─────────────────────────────────────────────────
    op.execute("ALTER TABLE datasources ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY datasources_isolation ON datasources
            USING (
                workspace_id = current_setting('app.current_workspace_id', true)::uuid
            )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS datasources_isolation ON datasources")
    op.execute("ALTER TABLE datasources DISABLE ROW LEVEL SECURITY")
    op.drop_table("datasources")
    op.execute("DROP TYPE IF EXISTS datasource_type")

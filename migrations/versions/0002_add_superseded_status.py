"""add 'superseded' to event_status enum

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-25

The monotonic ordering guard marks stale/out-of-order events `superseded`. That
value did not exist in the Stage 1 enum, so we add it here.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on some
    # Postgres versions, so we escape Alembic's surrounding transaction. IF NOT
    # EXISTS keeps re-runs idempotent.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE event_status ADD VALUE IF NOT EXISTS 'superseded'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE; removing an enum label requires
    # recreating the type and rewriting every dependent column. That is out of
    # scope for a downgrade, so this is intentionally a no-op (documented as
    # effectively irreversible).
    pass

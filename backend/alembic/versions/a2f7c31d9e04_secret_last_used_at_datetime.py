"""secret.last_used_at 列类型：String → DateTime

Revision ID: a2f7c31d9e04
Revises: 60fcac851ffa
Create Date: 2026-10-19

第一期模型里该列误声明为字符串（第二期权限链开始真正写入使用时间，修正类型）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a2f7c31d9e04"
down_revision = "60fcac851ffa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("secrets") as batch:
        batch.alter_column(
            "last_used_at",
            existing_type=sa.String(),
            type_=sa.DateTime(),
            existing_nullable=True,
            postgresql_using="last_used_at::timestamp",
        )


def downgrade() -> None:
    with op.batch_alter_table("secrets") as batch:
        batch.alter_column(
            "last_used_at",
            existing_type=sa.DateTime(),
            type_=sa.String(),
            existing_nullable=True,
        )

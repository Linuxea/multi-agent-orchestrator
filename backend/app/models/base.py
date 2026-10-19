"""SQLAlchemy 声明基类与公共混入（§4：id/created_at/updated_at/owner_id）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils import utcnow


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class IdMixin(TimestampMixin):
    """id 为带前缀的 ULID 字符串；owner_id 预留多用户（第一版本地用户）。"""

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(40), default="local", nullable=False)

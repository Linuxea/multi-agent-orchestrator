"""数据库引擎与会话工厂。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_config
from app.models import Base

_ENGINE: AsyncEngine | None = None
_SESSION_FACTORY: async_sessionmaker[AsyncSession] | None = None


def _default_json_serializer(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def get_engine() -> AsyncEngine:
    global _ENGINE
    if _ENGINE is None:
        cfg = get_config()
        # aiosqlite 用 timeout（busy timeout，秒）；psycopg 用 libpq 的 connect_timeout
        is_sqlite = cfg.database_url.startswith("sqlite")
        _ENGINE = create_async_engine(
            cfg.database_url,
            echo=False,
            json_serializer=_default_json_serializer,
            connect_args={"timeout": 30} if is_sqlite else {"connect_timeout": 30},
        )
        if _ENGINE.dialect.name == "sqlite":

            @event.listens_for(_ENGINE.sync_engine, "connect")
            def _fk_pragma(dbapi_conn: object, _record: object) -> None:
                cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()

    return _ENGINE


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _SESSION_FACTORY


async def session_scope() -> AsyncIterator[AsyncSession]:
    """提供事务性会话的上下文管理器用法（async with）。"""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def init_db() -> None:
    """建表（开发/测试用）。生产用 Alembic（见 backend/alembic）。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        await _ENGINE.dispose()
        _ENGINE = None
        _SESSION_FACTORY = None


async def check_db_ready() -> None:
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

"""第五期工作目录清理：保留期、活跃尝试保护、空父目录修剪、开关。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from app.enums import AttemptStatus
from app.ids import new_id
from app.maintenance import cleanup_workspaces
from app.models import Attempt, Task
from app.orchestrator.helpers import db_session

pytestmark = pytest.mark.asyncio


def _ws(rel: str, *, age_days: float = 0.0) -> str:
    from app.config import get_config

    base = get_config().data_dir / "workspaces" / rel
    base.mkdir(parents=True, exist_ok=True)
    (base / "out").mkdir(exist_ok=True)
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(base, (stamp, stamp))
    return str(base)


def _exists(p: str) -> bool:
    """同步存在性断言辅助（避免 async 测试内的阻塞 IO 告警）。"""
    return Path(p).exists()


async def _task_row() -> str:
    from app.enums import RunStatus
    from app.models import Run, Session

    async with db_session() as session:
        s = Session(id=new_id("sess"), title="m")
        session.add(s)
        await session.flush()
        run = Run(
            id=new_id("run"), session_id=s.id, trigger_message_id=new_id("msg"),
            status=RunStatus.completed,
        )
        session.add(run)
        await session.flush()
        task = Task(
            id=new_id("task"), run_id=run.id, key="T1", title="t", goal="",
            assigned_agent_id="agent_x",
        )
        session.add(task)
        await session.flush()
        return task.id


async def test_removes_expired_keeps_recent(env: Any) -> None:
    old = _ws("run1/T1/1", age_days=30)
    recent = _ws("run1/T1/2", age_days=1)
    async with db_session() as session:
        await env.settings.patch(session, {"workspace_retention_days": 14})

    summary = await cleanup_workspaces()
    assert summary["removed_dirs"] == 1
    assert not _exists(old)
    assert _exists(recent)
    # 保留的尝试目录其父目录不修剪
    assert _exists(str(Path(recent).parent))


async def test_prunes_empty_parents(env: Any) -> None:
    gone = _ws("run2/T1/1", age_days=30)
    async with db_session() as session:
        await env.settings.patch(session, {"workspace_retention_days": 7})
    summary = await cleanup_workspaces()
    assert summary["removed_dirs"] == 1
    assert summary["pruned_parents"] >= 2  # 任务目录 + 运行目录
    assert not _exists(gone)
    from app.config import get_config

    assert not (get_config().data_dir / "workspaces" / "run2").exists()


async def test_protects_active_attempt(env: Any) -> None:
    active = _ws("run3/T1/1", age_days=30)
    task_id = await _task_row()
    async with db_session() as session:
        session.add(Attempt(
            id=new_id("att"), task_id=task_id, number=1, agent_id="agent_x",
            status=AttemptStatus.running, workspace_path=active,
        ))
        await session.flush()
    async with db_session() as session:
        await env.settings.patch(session, {"workspace_retention_days": 7})

    summary = await cleanup_workspaces()
    assert summary["removed_dirs"] == 0
    assert _exists(active)


async def test_zero_or_missing_setting_disables(env: Any) -> None:
    expired = _ws("run4/T1/1", age_days=365)
    async with db_session() as session:
        await env.settings.patch(session, {"workspace_retention_days": 0})
    summary = await cleanup_workspaces()
    assert summary["disabled"] is True
    assert summary["removed_dirs"] == 0
    assert _exists(expired)

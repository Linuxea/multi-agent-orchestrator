"""维护任务（第五期增强）：工作目录保留清理（设置项 workspace_retention_days）。

- 保留期按尝试目录 mtime 计；到期目录整删（rmtree）。
- 跳过未终结尝试（queued/running/awaiting_approval）的目录。
- 删除后顺带清理空的任务/运行父目录。
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import time
from typing import Any

from sqlalchemy import select

from app.config import get_config
from app.orchestrator.helpers import db_session
from app.settings import SettingsService

logger = logging.getLogger(__name__)


async def cleanup_workspaces() -> dict[str, Any]:
    """执行一次清理，返回摘要（removed_dirs / pruned_parents / retention_days / disabled）。"""
    from app.enums import AttemptStatus
    from app.models import Attempt

    settings_service = SettingsService()
    async with db_session() as session:
        settings = await settings_service.get(session)
        result = await session.execute(
            select(Attempt.workspace_path).where(
                Attempt.status.in_(
                    [
                        AttemptStatus.queued,
                        AttemptStatus.running,
                        AttemptStatus.awaiting_approval,
                    ]
                )
            )
        )
        active_paths = {str(p) for p in result.scalars() if p}

    retention_days = int(settings.get("workspace_retention_days") or 0)
    out: dict[str, Any] = {
        "retention_days": retention_days,
        "removed_dirs": 0,
        "pruned_parents": 0,
        "disabled": retention_days <= 0,
    }
    root = get_config().data_dir / "workspaces"
    if retention_days <= 0 or not root.is_dir():
        return out

    cutoff = time.time() - retention_days * 86400
    removed = 0
    pruned = 0
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        for task_dir in sorted(run_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            for attempt_dir in sorted(task_dir.iterdir()):
                if not attempt_dir.is_dir():
                    continue
                if str(attempt_dir) in active_paths:
                    continue
                try:
                    if attempt_dir.stat().st_mtime < cutoff:
                        shutil.rmtree(attempt_dir)
                        removed += 1
                except OSError:
                    logger.exception("清理工作目录失败 %s", attempt_dir)
            if task_dir.is_dir() and not any(task_dir.iterdir()):
                with contextlib.suppress(OSError):
                    task_dir.rmdir()
                    pruned += 1
        if run_dir.is_dir() and not any(run_dir.iterdir()):
            with contextlib.suppress(OSError):
                run_dir.rmdir()
                pruned += 1
    out["removed_dirs"] = removed
    out["pruned_parents"] = pruned
    if removed:
        logger.info(
            "工作目录清理：删除 %d 个到期尝试目录（保留 %d 天）", removed, retention_days
        )
    return out

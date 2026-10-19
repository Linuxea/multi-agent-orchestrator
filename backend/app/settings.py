"""全局设置（§13.1）：单行 JSON 存储，带默认值。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.utils import utcnow

SETTINGS_KEY = "global"

DEFAULT_SETTINGS: dict[str, Any] = {
    "orchestrator_model_profile_id": None,  # 首个标记默认的配置（启动时回填）
    "worker_default_model_profile_id": None,
    "plan_approval_mode": "auto_if_under",
    "plan_auto_approve_cost_threshold": 0.5,
    "currency": "CNY",
    "default_run_budget": {"max_cost": 5.0, "max_wall_seconds": 3600},
    "global_parallel_limit": 6,
    "run_parallel_limit": 4,
    "max_tasks_per_plan": 20,
    "default_max_attempts": 3,
    "approval_timeout_minutes": 30,
    "daily_cost_limit": None,  # 关闭
    "workspace_retention_days": 14,
    "allow_custom_python_tools": False,
    "dangerous_command_list": [
        "sudo",
        "git push --force",
        "git push -f",
        "rm -rf /",
        "chmod -R 777 /",
    ],
    "access_password": None,
}


class SettingsService:
    async def get(self, session: AsyncSession) -> dict[str, Any]:
        result = await session.execute(
            select(AppSetting).where(AppSetting.key == SETTINGS_KEY)
        )
        row = result.scalar_one_or_none()
        merged = dict(DEFAULT_SETTINGS)
        if row is not None:
            merged.update(row.data or {})
        return merged

    async def patch(self, session: AsyncSession, changes: dict[str, Any]) -> dict[str, Any]:
        current = await self.get(session)
        unknown = set(changes) - set(DEFAULT_SETTINGS)
        if unknown:
            raise ValueError(f"未知设置项: {sorted(unknown)}")
        current.update(changes)
        result = await session.execute(
            select(AppSetting).where(AppSetting.key == SETTINGS_KEY)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = AppSetting(key=SETTINGS_KEY, data=current)
            session.add(row)
        else:
            row.data = current
            row.updated_at = utcnow()
        await session.flush()
        return current

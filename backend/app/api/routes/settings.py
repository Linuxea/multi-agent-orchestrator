"""全局设置 API（§13.1）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.bootstrap import get_container
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["settings"])


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    async with db_session() as session:
        return await get_container().settings.get(session)


class SettingsPatch(BaseModel):
    changes: dict[str, Any]


@router.patch("/settings")
async def patch_settings(body: SettingsPatch) -> dict[str, Any]:
    container = get_container()
    async with db_session() as session:
        try:
            updated = await container.settings.patch(session, body.changes)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        await container.events.emit(
            session,
            type="config.settings_changed",
            actor="user",
            run_id=None,
            payload={"entity": "settings", "entity_id": "global", "diff": body.changes},
        )
        return updated

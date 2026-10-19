"""尝试详情与追加指令（§12.2）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models import Attempt
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["attempts"])


@router.get("/attempts/{attempt_id}")
async def get_attempt(attempt_id: str) -> dict[str, Any]:
    async with db_session() as session:
        a = await session.get(Attempt, attempt_id)
        if a is None:
            raise HTTPException(404, "attempt 不存在")
        return {
            "id": a.id,
            "task_id": a.task_id,
            "number": a.number,
            "agent_id": a.agent_id,
            "agent_version": a.agent_version,
            "status": a.status.value,
            "brief": a.brief,
            "report": a.report,
            "review": a.review,
            "usage": a.usage,
            "workspace_path": a.workspace_path,
            "error": a.error,
            "counted": a.counted,
            "auto_retry_of": a.auto_retry_of,
        }


class InputBody(BaseModel):
    text: str


@router.post("/attempts/{attempt_id}/input", status_code=204)
async def inject_input(attempt_id: str, body: InputBody) -> None:
    """给运行中的 agent 追加指令（§10.9）。第一期 LiteExecutor 记录事件，热注入第二期实现。"""
    from app.bootstrap import get_container

    async with db_session() as session:
        a = await session.get(Attempt, attempt_id)
        if a is None:
            raise HTTPException(404, "attempt 不存在")
        container = get_container()
        await container.events.emit(
            session,
            type="attempt.input_injected",
            actor="user",
            run_id=None,
            attempt_id=attempt_id,
            payload={"text": body.text},
        )

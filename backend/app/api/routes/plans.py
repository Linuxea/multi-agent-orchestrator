"""计划确认与用户回答（§12.2）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.routes.runs import _run_or_404
from app.bootstrap import get_container
from app.models import Plan
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["plans"])


@router.get("/runs/{run_id}/plans/{version}")
async def get_plan(run_id: str, version: int) -> dict[str, Any]:
    from fastapi import HTTPException
    from sqlalchemy import select

    async with db_session() as session:
        await _run_or_404(session, run_id)
        result = await session.execute(
            select(Plan).where(Plan.run_id == run_id, Plan.version == version)
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            raise HTTPException(404, "计划版本不存在")
        return {
            "id": plan.id,
            "version": plan.version,
            "tasks_snapshot": plan.tasks_snapshot,
            "change_reason": plan.change_reason,
            "proposed_by": plan.proposed_by,
            "approved": plan.approved,
        }


class ApproveBody(BaseModel):
    modifications: dict[str, Any] | None = None


@router.post("/runs/{run_id}/plans/{version}/approve", status_code=204)
async def approve_plan(run_id: str, version: int, body: ApproveBody | None = None) -> None:
    await get_container().engine.approve_plan(run_id, version, (body.modifications if body else None))


class RejectBody(BaseModel):
    note: str = ""


@router.post("/runs/{run_id}/plans/{version}/reject", status_code=204)
async def reject_plan(run_id: str, version: int, body: RejectBody) -> None:
    await get_container().engine.reject_plan(run_id, version, body.note)


class AnswerBody(BaseModel):
    question_id: str | None = None
    answer: str


@router.post("/runs/{run_id}/answers", status_code=204)
async def submit_answer(run_id: str, body: AnswerBody) -> None:
    await get_container().engine.submit_answer(run_id, body.answer, body.question_id)

"""审批（§11、§12.2）：第一期数据模型与接口；触发链路第二期接入。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.bootstrap import get_container
from app.enums import ApprovalStatus, RememberScope
from app.models import Approval
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["approvals"])


@router.get("/approvals")
async def list_approvals(status: str = "pending") -> list[dict[str, Any]]:
    async with db_session() as session:
        result = await session.execute(
            select(Approval).where(Approval.status == status).order_by(Approval.created_at.desc())
        )
        return [
            {
                "id": a.id,
                "run_id": a.run_id,
                "attempt_id": a.attempt_id,
                "requested_by": a.requested_by,
                "tool_slug": a.tool_slug,
                "action": a.action,
                "arguments": a.arguments,
                "reason": a.reason,
                "risk_note": a.risk_note,
                "status": a.status.value,
            }
            for a in result.scalars().all()
        ]


class DecideBody(BaseModel):
    approved: bool
    note: str = ""
    remember: str = "once"


@router.post("/approvals/{approval_id}/decide", status_code=204)
async def decide(approval_id: str, body: DecideBody) -> None:
    container = get_container()
    async with db_session() as session:
        a = await session.get(Approval, approval_id)
        if a is None:
            raise HTTPException(404, "审批不存在")
        if a.status.value != "pending":
            raise HTTPException(409, f"审批已处理（{a.status.value}）")
        from app.utils import utcnow

        a.status = ApprovalStatus.approved if body.approved else ApprovalStatus.rejected
        a.decision_note = body.note
        a.remember = RememberScope(body.remember)
        a.decided_at = utcnow()
        await container.events.emit(
            session,
            type="approval.decided",
            actor="user",
            run_id=a.run_id,
            payload={
                "approval_id": a.id,
                "approved": body.approved,
                "note": body.note,
            },
        )
    # 唤醒等待中的执行器，并按 remember 记忆（once / this_run / always_for_agent）
    await container.scheduler.approvals.resolve(
        approval_id,
        approved=body.approved,
        note=body.note,
        remember=RememberScope(body.remember),
    )

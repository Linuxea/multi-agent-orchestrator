"""任务查询与人工干预操作（§10.9、§12.2）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.bootstrap import get_container
from app.enums import DecisionType, TaskStatus
from app.models import Attempt, Run, Task
from app.orchestrator.helpers import db_session, record_decision, transition_task

router = APIRouter(tags=["tasks"])


async def _task_or_404(session: Any, task_id: str) -> Task:
    task: Task | None = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task 不存在")
    return task


@router.get("/runs/{run_id}/tasks")
async def list_tasks(run_id: str) -> list[dict[str, Any]]:
    async with db_session() as session:
        result = await session.execute(
            select(Task).where(Task.run_id == run_id).order_by(Task.key)
        )
        return [
            {
                "id": t.id,
                "key": t.key,
                "title": t.title,
                "goal": t.goal,
                "status": t.status.value,
                "priority": t.priority,
                "depends_on": t.depends_on,
                "attempt_count": t.attempt_count,
                "max_attempts": t.max_attempts,
                "result_summary": t.result_summary,
                "result_artifact_ids": t.result_artifact_ids,
            }
            for t in result.scalars().all()
        ]


@router.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    async with db_session() as session:
        t = await _task_or_404(session, task_id)
        attempts = (
            await session.execute(
                select(Attempt).where(Attempt.task_id == task_id).order_by(Attempt.number)
            )
        ).scalars().all()
        return {
            "id": t.id,
            "run_id": t.run_id,
            "key": t.key,
            "title": t.title,
            "goal": t.goal,
            "status": t.status.value,
            "depends_on": t.depends_on,
            "attempt_count": t.attempt_count,
            "max_attempts": t.max_attempts,
            "result_summary": t.result_summary,
            "attempts": [
                {
                    "id": a.id,
                    "number": a.number,
                    "status": a.status.value,
                    "report": a.report,
                    "review": a.review,
                    "brief": a.brief,
                    "error": a.error,
                }
                for a in attempts
            ],
        }


@router.post("/tasks/{task_id}/cancel", status_code=204)
async def cancel_task(task_id: str) -> None:
    container = get_container()
    async with db_session() as session:
        t = await _task_or_404(session, task_id)
        if not t.status.is_terminal:
            await transition_task(
                session, container.events, t, TaskStatus.cancelled, reason="用户取消", actor="user"
            )
            await record_decision(
                session, container.events, run_id=t.run_id, type=DecisionType.skipped,
                task_id=t.id, task_key=t.key, summary=f"用户取消任务 {t.key}",
                rationale="用户手动取消", made_by="user",
            )
    await container.engine.wake(t.run_id, "task_cancelled_by_user")


@router.post("/tasks/{task_id}/skip", status_code=204)
async def skip_task(task_id: str) -> None:
    container = get_container()
    async with db_session() as session:
        t = await _task_or_404(session, task_id)
        if not t.status.is_terminal:
            await transition_task(
                session, container.events, t, TaskStatus.skipped, reason="用户跳过", actor="user"
            )
    ready = []

    run = await session.get(Run, t.run_id)
    if run is not None:
        ready = await container.engine._recompute_readiness(session, run)  # noqa: SLF001
    if ready:
        await container.engine.wake(t.run_id, "tasks_ready")


@router.post("/tasks/{task_id}/accept", status_code=204)
async def force_accept(task_id: str) -> None:
    """强制采纳（§10.9）：用户直接判定通过，记录为用户决策。"""
    container = get_container()
    async with db_session() as session:
        t = await _task_or_404(session, task_id)
        if t.status == TaskStatus.reported:
            await transition_task(
                session, container.events, t, TaskStatus.accepted, reason="用户强制采纳", actor="user"
            )
            await record_decision(
                session, container.events, run_id=t.run_id, type=DecisionType.review_passed,
                task_id=t.id, task_key=t.key, summary=f"{t.key} 用户强制采纳",
                rationale="用户手动采纳", made_by="user",
            )
    await container.engine.wake(t.run_id, "task_accepted_by_user")


@router.post("/tasks/{task_id}/extend", status_code=204)
async def extend_attempts(task_id: str, body: dict[str, int] | None = None) -> None:
    async with db_session() as session:
        t = await _task_or_404(session, task_id)
        extra = int((body or {}).get("add_attempts", 1))
        t.max_attempts += extra


class ReassignBody(BaseModel):
    agent_id: str


@router.post("/tasks/{task_id}/reassign", status_code=204)
async def reassign_task(task_id: str, body: ReassignBody) -> None:
    """手动改派（§10.9）：变更负责人，等待主 agent 重写任务单并派发。"""
    container = get_container()
    async with db_session() as session:
        t = await _task_or_404(session, task_id)
        from app.models import Agent

        agent = await session.get(Agent, body.agent_id)
        if agent is None:
            raise HTTPException(404, "agent 不存在")
        prev = await session.get(Agent, t.assigned_agent_id)
        t.assigned_agent_id = agent.id
        await session.flush()
        await container.events.emit(
            session,
            type="task.reassigned",
            actor="user",
            run_id=t.run_id,
            task_key=t.key,
            payload={
                "key": t.key,
                "from_agent": prev.slug if prev else "",
                "to_agent": agent.slug,
                "reason": "用户手动改派",
            },
        )
        await record_decision(
            session, container.events, run_id=t.run_id, type=DecisionType.reassigned,
            task_id=t.id, task_key=t.key, summary=f"{t.key} 用户改派给 {agent.slug}",
            rationale="用户手动改派", made_by="user",
        )
    await container.engine.wake(t.run_id, "task_reassigned_by_user")


@router.post("/tasks/{task_id}/redispatch", status_code=204)
async def redispatch_task(task_id: str) -> None:
    """编辑任务单后重派（§10.9）：置回 ready 并唤醒主 agent 重新派发。"""
    container = get_container()
    async with db_session() as session:
        t = await _task_or_404(session, task_id)
        if t.status in (TaskStatus.reported, TaskStatus.blocked, TaskStatus.needs_rework):
            await transition_task(
                session, container.events, t, TaskStatus.ready, reason="用户要求重派", actor="user"
            )
    await container.engine.wake(t.run_id, "task_redispatched_by_user")

"""运行查询与控制（§12.2）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.bootstrap import get_container
from app.models import Plan, Run, Task
from app.orchestrator.helpers import db_session
from app.schemas.common import Budget

router = APIRouter(tags=["runs"])


async def _run_or_404(session: Any, run_id: str) -> Run:
    run: Run | None = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "run 不存在")
    return run


@router.get("/runs")
async def list_runs(limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    async with db_session() as session:
        result = await session.execute(select(Run).order_by(Run.created_at.desc()).limit(limit))
        runs = result.scalars().all()
        return {
            "items": [
                {
                    "id": r.id,
                    "session_id": r.session_id,
                    "status": r.status.value,
                    "outcome": r.outcome.value if r.outcome else None,
                    "created_at": r.created_at.isoformat(),
                }
                for r in runs
            ],
            "next_cursor": None,
        }


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    async with db_session() as session:
        run = await _run_or_404(session, run_id)
        tasks = (
            await session.execute(select(Task).where(Task.run_id == run_id).order_by(Task.key))
        ).scalars().all()
        return {
            "id": run.id,
            "session_id": run.session_id,
            "status": run.status.value,
            "current_plan_version": run.current_plan_version,
            "budget": run.budget,
            "usage": run.usage,
            "final_answer": run.final_answer,
            "outcome": run.outcome.value if run.outcome else None,
            "summary_of_gaps": run.summary_of_gaps,
            "tasks": [
                {
                    "id": t.id,
                    "key": t.key,
                    "title": t.title,
                    "status": t.status.value,
                    "attempt_count": t.attempt_count,
                    "result_summary": t.result_summary,
                }
                for t in tasks
            ],
            "created_at": run.created_at.isoformat(),
        }


@router.post("/runs/{run_id}/pause", status_code=204)
async def pause_run(run_id: str) -> None:
    await get_container().engine.pause_run(run_id)


@router.post("/runs/{run_id}/resume", status_code=204)
async def resume_run(run_id: str) -> None:
    await get_container().engine.resume_run(run_id)


@router.post("/runs/{run_id}/cancel", status_code=204)
async def cancel_run(run_id: str, body: dict[str, str] | None = None) -> None:
    await get_container().engine.cancel_run(run_id, (body or {}).get("reason", ""))


class BudgetBody(BaseModel):
    max_cost: float | None = None
    max_wall_seconds: int | None = None
    max_turns: int | None = None


@router.post("/runs/{run_id}/budget", status_code=204)
async def extend_budget(run_id: str, body: BudgetBody) -> None:
    await get_container().engine.extend_budget(run_id, Budget(**body.model_dump()))


@router.get("/runs/{run_id}/plans")
async def list_plans(run_id: str) -> list[dict[str, Any]]:
    async with db_session() as session:
        await _run_or_404(session, run_id)
        result = await session.execute(
            select(Plan).where(Plan.run_id == run_id).order_by(Plan.version)
        )
        return [
            {
                "id": p.id,
                "version": p.version,
                "tasks_snapshot": p.tasks_snapshot,
                "change_reason": p.change_reason,
                "proposed_by": p.proposed_by,
                "approved": p.approved,
            }
            for p in result.scalars().all()
        ]


@router.get("/runs/{run_id}/decisions")
async def list_decisions(run_id: str) -> list[dict[str, Any]]:
    from app.models import Decision

    async with db_session() as session:
        await _run_or_404(session, run_id)
        result = await session.execute(
            select(Decision).where(Decision.run_id == run_id).order_by(Decision.created_at)
        )
        return [
            {
                "id": d.id,
                "type": d.type.value,
                "summary": d.summary,
                "rationale": d.rationale,
                "made_by": d.made_by.value,
                "task_id": d.task_id,
                "created_at": d.created_at.isoformat(),
            }
            for d in result.scalars().all()
        ]

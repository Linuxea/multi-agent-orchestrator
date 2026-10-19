"""引擎内部辅助：状态转换（含事件）、决策记录、DB 会话上下文。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_factory
from app.enums import DecisionMaker, DecisionType, RunStatus, TaskStatus
from app.events.service import EventService
from app.ids import new_id
from app.models import Decision, Run, Task
from app.orchestrator.states import InvalidTransition, check_run_transition, check_task_transition

TASK_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,19}$")


@asynccontextmanager
async def db_session() -> AsyncIterator[AsyncSession]:
    """事务性会话：正常提交，异常回滚。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def transition_task(
    session: AsyncSession,
    events: EventService,
    task: Task,
    to: TaskStatus,
    *,
    reason: str = "",
    actor: str = "system",
) -> Task:
    """合法转换并发 task.status_changed；非法则记录 engine.invalid_transition 后抛出。"""
    run_id = task.run_id
    try:
        check_task_transition(task.status, to)
    except InvalidTransition as e:
        await events.emit(
            session,
            type="engine.invalid_transition",
            actor="system",
            run_id=run_id,
            task_key=task.key,
            payload={
                "entity": "task",
                "entity_id": task.id,
                "from_status": e.current,
                "to_status": e.target,
                "actor": actor,
            },
        )
        raise
    old = task.status
    task.status = to
    await session.flush()
    await events.emit(
        session,
        type="task.status_changed",
        actor=actor,
        run_id=run_id,
        task_key=task.key,
        payload={
            "key": task.key,
            "from_status": old.value,
            "to_status": to.value,
            "reason": reason,
        },
    )
    return task


async def transition_run(
    session: AsyncSession,
    events: EventService,
    run: Run,
    to: RunStatus,
    *,
    reason: str = "",
    actor: str = "system",
) -> Run:
    try:
        check_run_transition(run.status, to)
    except InvalidTransition as e:
        await events.emit(
            session,
            type="engine.invalid_transition",
            actor="system",
            run_id=run.id,
            payload={
                "entity": "run",
                "entity_id": run.id,
                "from_status": e.current,
                "to_status": e.target,
                "actor": actor,
            },
        )
        raise
    old = run.status
    run.status = to
    await session.flush()
    await events.emit(
        session,
        type="run.status_changed",
        actor=actor,
        run_id=run.id,
        payload={"from_status": old.value, "to_status": to.value, "reason": reason},
    )
    return run


async def record_decision(
    session: AsyncSession,
    events: EventService,
    *,
    run_id: str,
    type: DecisionType | str,
    summary: str,
    rationale: str,
    made_by: DecisionMaker | str = DecisionMaker.orchestrator,
    task_id: str | None = None,
    task_key: str | None = None,
    alternatives: list[str] | None = None,
    inputs: list[str] | None = None,
) -> Decision:
    if isinstance(type, str):
        type = DecisionType(type)
    if isinstance(made_by, str):
        made_by = DecisionMaker(made_by)
    decision = Decision(
        id=new_id("dec"),
        run_id=run_id,
        task_id=task_id,
        type=type,
        summary=summary[:500],
        rationale=rationale,
        alternatives=alternatives or [],
        inputs=inputs or [],
        made_by=made_by,
    )
    session.add(decision)
    await session.flush()
    await events.emit(
        session,
        type="decision.recorded",
        actor=made_by.value,
        run_id=run_id,
        task_key=task_key,
        payload={
            "decision_id": decision.id,
            "type": type.value,
            "summary": decision.summary,
            "rationale": rationale,
            "made_by": made_by.value,
        },
    )
    return decision


def valid_task_key(key: str) -> bool:
    return bool(TASK_KEY_RE.match(key))

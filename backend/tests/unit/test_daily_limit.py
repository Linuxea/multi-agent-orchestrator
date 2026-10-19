"""第五期日成本限额：超限停启新尝试 + 当日一次 budget.exceeded(daily) 告警。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.enums import AgentStatus, AttemptStatus, RunStatus, TaskStatus
from app.ids import new_id
from app.models import Agent, Attempt, Event, ModelProfile, Run, Session, Task
from app.orchestrator.helpers import db_session
from app.runtime.types import RunContext

pytestmark = pytest.mark.asyncio


async def _seed(container: Any) -> tuple[str, str]:
    """Run(running) + Agent + Task(queued) + 已花费尝试 + 待排尝试。返回 (task_id, agent_id)。"""
    async with db_session() as session:
        profile = ModelProfile(
            id=new_id("mp"), name="dl", base_url="http://127.0.0.1:9/v1", model="m"
        )
        session.add(profile)
        agent = Agent(
            id=new_id("agent"), slug="dl", name="dl", description="", role_detail="",
            capability_tags=[], system_prompt="sp", executor="lite",
            model_profile_id=profile.id, status=AgentStatus.active,
        )
        session.add(agent)
        s = Session(id=new_id("sess"), title="dl")
        session.add(s)
        await session.flush()
        run = Run(
            id=new_id("run"), session_id=s.id, trigger_message_id=new_id("msg"),
            status=RunStatus.running,
        )
        session.add(run)
        await session.flush()
        task = Task(
            id=new_id("task"), run_id=run.id, key="T1", title="t", goal="",
            assigned_agent_id=agent.id, status=TaskStatus.queued,
        )
        session.add(task)
        await session.flush()
        spent = Attempt(
            id=new_id("att"), task_id=task.id, number=1, agent_id=agent.id,
            status=AttemptStatus.reported,
            usage={"cost": 2.0, "input_tokens": 100, "output_tokens": 50},
        )
        spent.started_at = _utcnow()
        pending = Attempt(
            id=new_id("att"), task_id=task.id, number=2, agent_id=agent.id,
            status=AttemptStatus.queued,
            brief={
                "task_key": "T1", "agent_slug": "dl", "objective": "o",
                "acceptance_criteria": ["a"],
                "deliverable": {"format": "markdown"},
                "selection_rationale": "t",
            },
        )
        session.add_all([spent, pending])
        await session.flush()
    container.scheduler.engine = None
    return task.id, agent.id


def _utcnow() -> Any:
    from app.utils import utcnow

    return utcnow()


async def _daily_events() -> list[Event]:
    async with db_session() as session:
        result = await session.execute(
            select(Event).where(Event.type == "budget.exceeded")
        )
        return [e for e in result.scalars().all() if e.payload.get("level") == "daily"]


class _EmptyExecutor:
    """空转执行器：让 schedule() 的启动路径走通而不真正调模型。"""

    name = "lite"

    async def start(self, ctx: RunContext) -> Any:
        return
        yield  # pragma: no cover

    async def send_input(self, ctx: RunContext, text: str) -> None: ...

    async def interrupt(self, attempt_id: str) -> None: ...

    def resume(self, ctx: RunContext) -> Any:
        return None


async def test_over_limit_blocks_and_notifies_once(env: Any) -> None:
    task_id, agent_id = await _seed(env)
    async with db_session() as session:
        await env.settings.patch(session, {"daily_cost_limit": 1.0})

    await env.scheduler.schedule()
    await env.scheduler.schedule()  # 第二次不再重复告警
    assert not env.scheduler._running  # noqa: SLF001

    events = await _daily_events()
    assert len(events) == 1
    assert events[0].run_id is None
    assert events[0].payload["usage"]["cost"] >= 2.0

    async with db_session() as session:
        result = await session.execute(
            select(Attempt).where(
                Attempt.task_id == task_id, Attempt.status == AttemptStatus.queued
            )
        )
        assert len(list(result.scalars().all())) == 1
    _ = agent_id


async def test_under_limit_or_disabled_schedules(env: Any) -> None:
    task_id, _agent_id = await _seed(env)
    async with db_session() as session:
        await env.settings.patch(session, {"daily_cost_limit": 10.0})  # 未超
    env.executors.register(_EmptyExecutor())

    await env.scheduler.schedule()
    await _quiesce(env)
    async with db_session() as session:
        result = await session.execute(
            select(Attempt).where(
                Attempt.task_id == task_id, Attempt.status == AttemptStatus.running
            )
        )
        assert len(list(result.scalars().all())) == 1
    assert not await _daily_events()


async def test_none_limit_disables(env: Any) -> None:
    task_id, _agent_id = await _seed(env)
    async with db_session() as session:
        await env.settings.patch(session, {"daily_cost_limit": None})
    env.executors.register(_EmptyExecutor())

    await env.scheduler.schedule()
    await _quiesce(env)
    async with db_session() as session:
        result = await session.execute(
            select(Attempt).where(
                Attempt.task_id == task_id, Attempt.status == AttemptStatus.running
            )
        )
        assert len(list(result.scalars().all())) == 1


async def _quiesce(env: Any) -> None:
    import asyncio

    for _ in range(200):
        if not env.scheduler._running:  # noqa: SLF001
            return
        await asyncio.sleep(0.02)

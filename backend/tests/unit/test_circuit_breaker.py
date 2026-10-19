"""第五期引擎熔断：窗口崩溃计数、调度闸门、冷却自动合闸、成功回报清零。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select

from app.enums import AgentStatus, AttemptStatus, RunStatus, TaskStatus
from app.ids import new_id
from app.models import Agent, Attempt, Event, Run, Session, Task
from app.orchestrator.helpers import db_session
from app.runtime.types import RunContext

pytestmark = pytest.mark.asyncio

THRESHOLD = 3


async def _seed(container: Any, *, crashed: int) -> tuple[list[str], str, str]:
    """建 Run(running) → Task → 若干 running 尝试（待崩溃）。

    另预置一条 auto_retry_of 尝试，使崩溃路径不再触发自动重试（隔离熔断逻辑）。
    返回 (尝试 id 列表, task_id, agent_id)。
    """
    async with db_session() as session:
        from app.models import ModelProfile

        profile = ModelProfile(
            id=new_id("mp"), name="cb", base_url="http://127.0.0.1:9/v1", model="m"
        )
        session.add(profile)
        agent = Agent(
            id=new_id("agent"), slug="cb", name="cb", description="", role_detail="",
            capability_tags=[], system_prompt="sp", executor="lite",
            model_profile_id=profile.id, status=AgentStatus.active,
        )
        session.add(agent)
        s = Session(id=new_id("sess"), title="cb")
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
            assigned_agent_id=agent.id, status=TaskStatus.running,
        )
        session.add(task)
        await session.flush()
        session.add(Attempt(
            id=new_id("att"), task_id=task.id, number=99, agent_id=agent.id,
            status=AttemptStatus.cancelled, auto_retry_of="att-none",
        ))
        ids: list[str] = []
        for n in range(1, crashed + 1):
            att = Attempt(
                id=new_id("att"), task_id=task.id, number=n, agent_id=agent.id,
                status=AttemptStatus.running,
            )
            session.add(att)
            ids.append(att.id)
        await session.flush()
        container.scheduler.engine = None  # 隔离：不唤醒主 agent
        return ids, task.id, agent.id


async def _add_queued(task_id: str, agent_id: str) -> str:
    async with db_session() as session:
        att = Attempt(
            id=new_id("att"), task_id=task_id, number=42, agent_id=agent_id,
            status=AttemptStatus.queued,
        )
        session.add(att)
        await session.flush()
        return att.id


async def _global_events(etype: str) -> list[Event]:
    async with db_session() as session:
        result = await session.execute(select(Event).where(Event.type == etype))
        return list(result.scalars().all())


@pytest.fixture(autouse=True)
def cb_config(env: Any) -> None:
    from app.config import get_config

    c = get_config()
    c.circuit_breaker_crashes = THRESHOLD
    c.circuit_breaker_window_seconds = 300.0
    c.circuit_breaker_cooldown_seconds = 300.0


async def test_opens_circuit_and_blocks_scheduling(env: Any) -> None:
    ids, task_id, agent_id = await _seed(env, crashed=THRESHOLD)
    scheduler = env.scheduler

    for att_id in ids:
        await scheduler.handle_crashed_attempt(att_id, "boom")

    assert scheduler.circuit_open is True
    opened = await _global_events("engine.circuit_open")
    assert len(opened) == 1
    assert "崩溃" in opened[0].payload["detail"]
    assert opened[0].run_id is None  # 全局事件

    # 开闸后新排队的尝试不被启动
    queued_id = await _add_queued(task_id, agent_id)
    await scheduler.schedule()
    await asyncio.sleep(0)  # 让潜在启动任务有机会注册（不应发生）
    assert not scheduler._running  # noqa: SLF001
    async with db_session() as session:
        attempt = await session.get(Attempt, queued_id)
        assert attempt is not None and attempt.status == AttemptStatus.queued


async def test_below_threshold_no_circuit(env: Any) -> None:
    ids, _, _ = await _seed(env, crashed=THRESHOLD - 1)
    for att_id in ids:
        await env.scheduler.handle_crashed_attempt(att_id, "boom")
    assert env.scheduler.circuit_open is False
    assert not await _global_events("engine.circuit_open")


async def test_cooldown_auto_close_emits_recovered(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.orchestrator.scheduler as sched_mod

    ids, _, _ = await _seed(env, crashed=THRESHOLD)
    scheduler = env.scheduler
    for att_id in ids:
        await scheduler.handle_crashed_attempt(att_id, "boom")
    assert scheduler.circuit_open is True

    base_now = sched_mod._now()
    monkeypatch.setattr(sched_mod, "_now", lambda: base_now + 10_000.0)
    assert await scheduler._circuit_gate() is True
    assert scheduler.circuit_open is False
    recovered = await _global_events("engine.recovered")
    assert recovered and "熔断冷却" in recovered[-1].payload["detail"]


async def test_reported_resets_window(env: Any) -> None:
    ids, task_id, agent_id = await _seed(env, crashed=THRESHOLD)
    scheduler = env.scheduler
    # 前两次崩溃不足阈值
    for att_id in ids[: THRESHOLD - 1]:
        await scheduler.handle_crashed_attempt(att_id, "boom")
    assert not await _global_events("engine.circuit_open")

    # 一次成功回报清零窗口（直接走 _handle_reported 的清零分支）
    async with db_session() as session:
        task = await session.get(Task, (await session.get(Attempt, ids[0])).task_id)  # type: ignore[union-attr]
        reported = Attempt(
            id=new_id("att"), task_id=task.id, number=50,  # type: ignore[union-attr]
            agent_id=(await session.get(Attempt, ids[0])).agent_id,  # type: ignore[union-attr]
            brief={
                "task_key": "T1", "agent_slug": "cb", "objective": "o",
                "acceptance_criteria": ["a"],
                "deliverable": {"format": "markdown"},
                "selection_rationale": "t",
            },
            status=AttemptStatus.reported,
        )
        session.add(reported)
        task.status = TaskStatus.running  # type: ignore[assignment]
        await session.flush()
        reported_id = reported.id
    ctx = RunContext(
        run_id="run_x", task_key="T1", attempt_id=reported_id, attempt_no=50,
        agent_slug="cb", workspace="/tmp", system_prompt="", first_input="",
        budget={}, model=None,  # type: ignore[arg-type]
    )
    await scheduler._handle_reported(ctx, {"status": "done", "summary": "ok"})
    assert len(scheduler._crash_times) == 0  # noqa: SLF001

    # 再崩溃两次（窗口已清零）仍不开闸
    for att_id in ids[THRESHOLD - 1 :]:
        await scheduler.handle_crashed_attempt(att_id, "boom")
    assert not await _global_events("engine.circuit_open")

"""第五期会话恢复：调度器侧——EvSessionBound 落库与自动重试的会话继承。"""

from __future__ import annotations

from typing import Any

import pytest

from app.enums import AgentStatus, AttemptStatus, RunStatus, TaskStatus
from app.ids import new_id
from app.models import Agent, Attempt, ModelProfile, Run, Session, Task
from app.orchestrator.helpers import db_session
from app.runtime.types import EvSessionBound, RunContext
from app.schemas.common import Usage

pytestmark = pytest.mark.asyncio

BRIEF: dict[str, Any] = {
    "task_key": "T1",
    "agent_slug": "w",
    "objective": "完成测试任务",
    "acceptance_criteria": ["产出报告"],
    "deliverable": {"format": "markdown", "name": "report.md"},
    "selection_rationale": "测试指派",
}


async def _seed_chain(container: Any) -> tuple[str, str, str]:
    """建 Run(running) → Task(blocked) → 尝试1(crashed, 有会话 ID) + 尝试2(queued, 自动重试)。"""
    async with db_session() as session:
        profile = ModelProfile(
            id=new_id("mp"), name="p", base_url="http://127.0.0.1:9/v1", model="m"
        )
        session.add(profile)
        agent = Agent(
            id=new_id("agent"),
            slug="w",
            name="w",
            description="",
            role_detail="",
            capability_tags=[],
            system_prompt="sp",
            executor="sdk",
            model_profile_id=profile.id,
            status=AgentStatus.active,
        )
        session.add(agent)
        s = Session(id=new_id("sess"), title="t")
        session.add(s)
        await session.flush()
        run = Run(
            id=new_id("run"),
            session_id=s.id,
            trigger_message_id=new_id("msg"),
            status=RunStatus.running,
        )
        session.add(run)
        await session.flush()
        task = Task(
            id=new_id("task"),
            run_id=run.id,
            key="T1",
            title="t",
            goal="",
            assigned_agent_id=agent.id,
            status=TaskStatus.queued,  # 自动重试链路先转 queued 再物化
        )
        session.add(task)
        await session.flush()
        att1 = Attempt(
            id=new_id("att"),
            task_id=task.id,
            number=1,
            agent_id=agent.id,
            brief=dict(BRIEF),
            status=AttemptStatus.crashed,
            executor_session_id="sess-first",
        )
        att2 = Attempt(
            id=new_id("att"),
            task_id=task.id,
            number=2,
            agent_id=agent.id,
            brief=dict(BRIEF),
            status=AttemptStatus.queued,
            auto_retry_of=att1.id,
        )
        session.add_all([att1, att2])
        await session.flush()
        return run.id, att1.id, att2.id


async def test_translate_persists_executor_session_id(env: Any) -> None:
    _, _, att2 = await _seed_chain(env)
    scheduler = env.scheduler
    ctx = RunContext(
        run_id="run_x", task_key="T1", attempt_id=att2, attempt_no=2, agent_slug="w",
        workspace="/tmp", system_prompt="", first_input="", budget={}, model=None,  # type: ignore[arg-type]
    )
    await scheduler._translate_event(ctx, EvSessionBound(session_id="sess-9"), Usage())
    async with db_session() as session:
        attempt = await session.get(Attempt, att2)
        assert attempt is not None
        assert attempt.executor_session_id == "sess-9"


async def test_materialize_inherits_source_session(env: Any) -> None:
    run_id, _att1, att2 = await _seed_chain(env)
    ctx = await env.scheduler._materialize(att2)
    assert ctx is not None
    assert ctx.resume_session_id == "sess-first"
    assert ctx.executor == "sdk"


async def test_materialize_without_retry_has_no_resume(env: Any) -> None:
    """非自动重试尝试不携带恢复会话。"""
    async with db_session() as session:
        profile = ModelProfile(
            id=new_id("mp"), name="p2", base_url="http://127.0.0.1:9/v1", model="m"
        )
        session.add(profile)
        agent = Agent(
            id=new_id("agent"), slug="w2", name="w2", description="", role_detail="",
            capability_tags=[], system_prompt="sp", executor="lite",
            model_profile_id=profile.id, status=AgentStatus.active,
        )
        session.add(agent)
        s = Session(id=new_id("sess"), title="t")
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
        att = Attempt(
            id=new_id("att"), task_id=task.id, number=1, agent_id=agent.id,
            brief=dict(BRIEF, agent_slug="w2", task_key="T1"),
            status=AttemptStatus.queued,
        )
        session.add(att)
        await session.flush()
        att_id = att.id

    ctx = await env.scheduler._materialize(att_id)
    assert ctx is not None
    assert ctx.resume_session_id is None


async def test_run_attempt_prefers_resume(env: Any) -> None:
    """_run_attempt 对带恢复会话的尝试优先调用 executor.resume。"""
    _, _att1, att2 = await _seed_chain(env)

    calls: dict[str, Any] = {}

    class ProbeExecutor:
        name = "sdk"  # 覆盖注册为 sdk：DB 枚举只允许 sdk/lite

        async def _empty(self) -> Any:
            return
            yield  # pragma: no cover - 使其成为异步生成器

        async def start(self, ctx: RunContext) -> Any:
            calls["start"] = True
            async for ev in self._empty():
                yield ev

        def resume(self, ctx: RunContext) -> Any:
            calls["resume"] = ctx.resume_session_id
            return self._empty()

        async def send_input(self, ctx: RunContext, text: str) -> None: ...

        async def interrupt(self, attempt_id: str) -> None: ...

    env.executors.register(ProbeExecutor())
    async with db_session() as session:
        agent_id = (await session.get(Attempt, att2)).agent_id  # type: ignore[union-attr]
        agent = await session.get(Agent, agent_id)
        assert agent is not None and agent.executor.value == "sdk"

    await env.scheduler._run_attempt(att2)
    # 调度器凭 resume 会话优先续跑，未直接走 start
    assert calls == {"resume": "sess-first"}

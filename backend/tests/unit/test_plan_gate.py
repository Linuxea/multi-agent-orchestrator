"""计划审批门槛测试：awaiting_plan_approval 期间修订与派发必须被拒。

背景（真实事故）：模型在计划等待用户确认期间经 revise_plan「刷新任务状态」
解锁任务并 dispatch，run 却停在待审批、调度器不启动——形同卡死；
用户会话内说「确认」不构成系统批准，模型误解后绕过了审批。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.enums import RunStatus
from app.ids import new_id
from app.models import Message, Run, Session
from app.orchestrator.engine import ControlToolError, WakeState
from app.orchestrator.helpers import db_session


async def _make_run(env: Any, status: RunStatus, suffix: str) -> str:
    async with db_session() as s:
        sess = Session(id=f"sess_gate_{suffix}", title="审批门槛")
        s.add(sess)
        await s.flush()
        msg = Message(id=new_id("msg"), session_id=sess.id, role="user", content="测试")
        s.add(msg)
        await s.flush()
        run = Run(
            id=new_id("run"),
            session_id=sess.id,
            trigger_message_id=msg.id,
            status=status,
            current_plan_version=1,
        )
        s.add(run)
        await s.flush()
        return run.id


async def _awaiting_run(env: Any) -> str:
    return await _make_run(env, RunStatus.awaiting_plan_approval, "a")


async def test_revise_plan_blocked_while_awaiting(env: Any) -> None:
    run_id = await _awaiting_run(env)
    ws = WakeState(run_id=run_id)
    with pytest.raises(ControlToolError, match="等待用户确认"):
        await env.engine._tool_revise_plan(
            ws, {"changes": {"remove_task_keys": ["T1"]}, "rationale": "r"}
        )


async def test_dispatch_blocked_while_awaiting(env: Any) -> None:
    run_id = await _awaiting_run(env)
    ws = WakeState(run_id=run_id)
    with pytest.raises(ControlToolError, match="等待用户确认"):
        await env.engine._tool_dispatch(ws, {"task_key": "T1", "rationale": "r"})


async def test_not_awaiting_states_not_blocked(env: Any) -> None:
    """非待审批状态（如 planning）不受门槛影响——参数错误而非门槛错误。"""
    run_id = await _make_run(env, RunStatus.planning, "p")
    ws = WakeState(run_id=run_id)
    # planning 状态下 dispatch 会因任务不存在而拒（而非审批门槛）
    with pytest.raises(ControlToolError, match="任务不存在"):
        await env.engine._tool_dispatch(ws, {"task_key": "NOPE", "rationale": "r"})

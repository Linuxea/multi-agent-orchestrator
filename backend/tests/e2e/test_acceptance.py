"""第一期验收清单测试（§14.2）。"""

from __future__ import annotations

from pathlib import Path as _P

import pytest
from sqlalchemy import select

import tests.scriptutil as S
from app.enums import TaskStatus
from app.models import Attempt, Task
from app.orchestrator.engine import WakeState
from app.orchestrator.helpers import db_session, transition_task
from app.orchestrator.states import InvalidTransition
from tests.conftest import Harness

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# 验收：非法转换被拒绝并记录 engine.invalid_transition
# ----------------------------------------------------------------------
async def test_invalid_transition_rejected_and_recorded(platform: Harness) -> None:
    h = platform
    await h.agent("worker", "执行员", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    run_id, _ = await h.start()
    async with db_session() as session:
        task = Task(
            id="task_x", run_id=run_id, key="T1", title="t",
            assigned_agent_id="agent_x", status=TaskStatus.accepted,
        )
        session.add(task)
        await session.flush()
        with pytest.raises(InvalidTransition):
            await transition_task(
                session, h.container.events, task, TaskStatus.ready, actor="system"
            )
    events = await h.events(run_id, ["engine.invalid_transition"])
    assert events, "应记录 engine.invalid_transition"
    assert events[0].payload["from_status"] == "accepted"
    assert events[0].payload["to_status"] == "ready"


# ----------------------------------------------------------------------
# 验收：派发注册表外 / 非 active 的 agent 被拒绝，错误原因返回给模型
# ----------------------------------------------------------------------
async def test_propose_plan_rejects_unknown_and_inactive_agent(platform: Harness) -> None:
    h = platform
    await h.agent("worker", "执行员", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    await h.agent("ghost-off", "停用者", status="disabled")
    run_id, _ = await h.start()
    engine = h.container.engine
    ws = WakeState(run_id=run_id)

    r1 = await engine._execute_control_tool(
        ws, "propose_plan",
        {"tasks": [S.task_spec("T1", "no-such-agent")], "rationale": "测试"},
    )
    assert "error" in r1 and "不可用" in r1["error"]

    r2 = await engine._execute_control_tool(
        ws, "propose_plan",
        {"tasks": [S.task_spec("T1", "ghost-off")], "rationale": "测试"},
    )
    assert "error" in r2

    rejected = await h.events(run_id, ["orchestrator.tool_rejected"])
    assert len(rejected) == 2
    assert "no-such-agent" in rejected[0].payload["reason"]


# ----------------------------------------------------------------------
# 验收：缺少 rationale 的控制工具调用被拒绝
# ----------------------------------------------------------------------
async def test_missing_rationale_rejected(platform: Harness) -> None:
    h = platform
    run_id, _ = await h.start()
    engine = h.container.engine
    ws = WakeState(run_id=run_id)

    result = await engine._execute_control_tool(ws, "search_agents", {"query": "调研"})
    assert "error" in result and "rationale" in result["error"]
    rejected = await h.events(run_id, ["orchestrator.tool_rejected"])
    assert rejected and "rationale" in rejected[0].payload["reason"]

    # 超长 rationale 也被拒绝
    result2 = await engine._execute_control_tool(
        ws, "search_agents", {"query": "调研", "rationale": "长" * 301}
    )
    assert "error" in result2


# ----------------------------------------------------------------------
# 验收：4 个无依赖任务在并行上限 2 时最多 2 个同时运行
# ----------------------------------------------------------------------
async def test_parallel_limit_two(platform: Harness) -> None:
    h = platform
    await h.patch_settings({"run_parallel_limit": 2})
    await h.agent(
        "worker", "执行员",
        budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30},
        max_concurrency=4,
    )
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                S.step_tools(
                    S.propose_plan(*[S.task_spec(f"T{i}", "worker") for i in range(1, 5)])
                ),
                S.step_tools(*[S.dispatch(f"T{i}", "worker") for i in range(1, 5)]),
                S.step_text(),
                *[S.step_tools(S.read_artifact(f"T{i}"), S.review_pass(f"T{i}")) for i in range(1, 5)],
                S.step_text(),
                S.step_tools(S.finalize()),
                S.step_text(),
            ),
            S.w_scope(
                "worker",
                *[
                    {
                        "tool_calls": [
                            {
                                "name": "submit_report",
                                "input": {
                                    "status": "done",
                                    "summary": f"完成 {i}",
                                    "deliverables": [{"name": f"r{i}.md", "content": f"# {i}", "summary": "ok"}],
                                    "criteria_self_check": [{"criterion": S.CRITERION, "met": True}],
                                },
                            }
                        ],
                        "stream_delay": 0.25,
                    }
                    for i in range(1, 5)
                ],
            ),
        )
    )
    run_id, _ = await h.start("并行执行四个任务")
    assert await h.wait(run_id, timeout=40)

    events = await h.events(run_id, ["task.status_changed"])
    running = 0
    max_running = 0
    started = 0
    for e in sorted(events, key=lambda x: x.seq or 0):
        p = e.payload
        if p.get("to_status") == "running":
            running += 1
            started += 1
            max_running = max(max_running, running)
        elif p.get("to_status") in ("reported", "blocked", "cancelled"):
            running -= 1
    assert started == 4, "四个任务都应执行"
    assert max_running <= 2, f"并行上限 2 被突破：{max_running}"
    tasks = await h.tasks(run_id)
    assert all(t.status.value == "accepted" for t in tasks.values())


# ----------------------------------------------------------------------
# 验收：依赖任务 accepted 后下游自动 ready（happy path 已覆盖语义，这里单列断言）
# ----------------------------------------------------------------------
async def test_dependency_ready_after_accept(platform: Harness) -> None:
    h = platform
    await h.agent("worker", "执行员", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                S.step_tools(
                    S.propose_plan(
                        S.task_spec("T1", "worker"),
                        S.task_spec("T2", "worker", depends_on=["T1"]),
                    )
                ),
                S.step_tools(S.dispatch("T1", "worker")),
                S.step_text(),
                S.step_tools(S.read_artifact("T1"), S.review_pass("T1")),
                S.step_text(),
                S.step_tools(S.dispatch("T2", "worker")),
                S.step_text(),
                S.step_tools(S.read_artifact("T2"), S.review_pass("T2")),
                S.step_text(),
                S.step_tools(S.finalize()),
                S.step_text(),
            ),
            S.w_scope(
                "worker",
                S.step_tools(S.report_done("完成 A")),
                S.step_tools(S.report_done("完成 B，内容与结构均不同，长度也明显更长以便区分")),
            ),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=30)
    tasks = await h.tasks(run_id)
    assert tasks["T2"].status.value == "accepted"

    events = await h.events(run_id, ["task.status_changed"])
    seq_t1_accepted = next(
        e.seq for e in events
        if e.payload.get("key") == "T1" and e.payload.get("to_status") == "accepted"
    )
    t2_pending_to_ready = [
        e for e in events
        if e.payload.get("key") == "T2" and e.payload.get("to_status") == "ready"
    ]
    assert t2_pending_to_ready
    assert seq_t1_accepted < t2_pending_to_ready[0].seq


# ----------------------------------------------------------------------
# 验收：达到最大尝试次数后任务 failed，并出现在"未完成"小节
# ----------------------------------------------------------------------
async def test_max_attempts_failed_in_gaps(platform: Harness) -> None:
    h = platform
    await h.agent("worker", "执行员", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    summaries = [
        "第一版只覆盖了 2 个来源，缺一个",
        "第二版换了一组来源但仍不完整，另外数据口径也有问题，需要再确认",
        "第三版尝试用替代数据源补齐，但仍差最后一个数字",
    ]
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                S.step_tools(S.propose_plan(S.task_spec("T1", "worker"))),
                S.step_tools(S.dispatch("T1", "worker")),
                S.step_text(),
                S.step_tools(S.review_fail("T1")),
                S.step_tools(S.dispatch("T1", "worker", rework_feedback="补充缺失来源")),
                S.step_text(),
                S.step_tools(S.review_fail("T1")),
                S.step_tools(S.dispatch("T1", "worker", rework_feedback="再次补充，换用替代数据")),
                S.step_text(),
                S.step_tools(S.review_fail("T1")),
                S.step_text(),
                S.step_tools(S.finalize(gaps="T1 三次尝试均未达标")),
                S.step_text(),
            ),
            S.w_scope(
                "worker",
                *[S.step_tools(S.report_done(s)) for s in summaries],
            ),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=40)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "failed"
    assert tasks["T1"].attempt_count == 3
    run = await h.run_row(run_id)
    # 唯一任务三次失败、无任何任务被采纳 → outcome=failed
    assert run.outcome and run.outcome.value == "failed"
    assert "T1" in run.summary_of_gaps

    # 第四次派发应被引擎拒绝（任务已终态 failed）
    ws = WakeState(run_id=run_id)
    result = await h.container.engine._execute_control_tool(
        ws, "dispatch",
        dict(S.dispatch("T1", "worker")[1], rationale="第四次尝试"),
    )
    assert "error" in result and "failed" in result["error"]


# ----------------------------------------------------------------------
# 验收：后端重启后，运行中的运行能恢复并完成
# ----------------------------------------------------------------------
async def test_restart_recovery(platform: Harness) -> None:
    h = platform
    worker = await h.agent(
        "worker", "执行员", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30}
    )
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                S.step_text("收到恢复通知，等待任务结果。"),  # 容纳 engine_recovered 首次唤醒
                S.step_tools(S.read_artifact("T1"), S.review_pass("T1")),
                S.step_text("复核完成"),
                S.step_tools(S.finalize()),
                S.step_text(),
            ),
            S.w_scope("worker", S.step_tools(S.report_done("重启后完成"))),
        )
    )
    # 直接在数据库中捏造"服务崩溃现场"：run running + task running + attempt running
    async with db_session() as session:
        from app.ids import new_id
        from app.models import Message, Run
        from app.models import Session as DbSession

        sess = DbSession(id=new_id("sess"), title="恢复测试")
        session.add(sess)
        await session.flush()
        msg = Message(id=new_id("msg"), session_id=sess.id, role="user", content="恢复测试需求")
        session.add(msg)
        await session.flush()
        run = Run(
            id=new_id("run"),
            session_id=sess.id,
            trigger_message_id=msg.id,
            status="running",
            current_plan_version=1,
            budget={"max_cost": 5.0, "max_wall_seconds": 3600},
        )
        session.add(run)
        await session.flush()
        task = Task(
            id=new_id("task"), run_id=run.id, key="T1", title="恢复任务",
            assigned_agent_id=worker.id, status="running", attempt_count=1,
        )
        session.add(task)
        await session.flush()
        session.add(
            Attempt(
                id=new_id("att"), task_id=task.id, number=1, agent_id=worker.id,
                agent_version=worker.version,
                brief={
                    "task_key": "T1",
                    "agent_slug": "worker",
                    "objective": "恢复测试任务",
                    "context": "服务重启前的现场",
                    "acceptance_criteria": [S.CRITERION],
                    "deliverable": {"format": "markdown", "name": "report.md", "notes": ""},
                    "selection_rationale": "测试",
                },
                status="running",
                counted=True,
            )
        )

    # 模拟重启：丢弃全部内存态，重建容器后执行恢复
    from app.bootstrap import reset_container

    fresh = reset_container()
    await fresh.recover()
    h.container = fresh

    assert await fresh.engine.quiesce(run.id, timeout=30)
    tasks_after = await h.tasks(run.id)
    assert tasks_after["T1"].status.value == "accepted"
    run_after = await h.run_row(run.id)
    assert run_after.status.value == "completed"

    async with db_session() as session:
        attempts = (
            await session.execute(select(Attempt).where(Attempt.task_id == tasks_after["T1"].id))
        ).scalars().all()
    assert len(attempts) == 2
    assert attempts[0].status.value == "crashed"
    assert attempts[1].auto_retry_of == attempts[0].id


# ----------------------------------------------------------------------
# 验收：仅凭事件流能重建出与状态表一致的运行状态（属性测试）
# ----------------------------------------------------------------------
async def test_event_stream_rebuild_matches_state(platform: Harness) -> None:
    h = platform
    await h.agent("web-researcher", "调研员", prompt="你是网络调研员（测试）。", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    await h.agent("writer", "撰稿人", prompt="你是撰稿人（测试）。", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    h.load_yaml(str((_P(__file__).parent.parent / "scripts" / "multi_domain_research.yaml").resolve()))
    run_id, _ = await h.start("调研并写简报")
    assert await h.wait(run_id, timeout=30)

    from app.orchestrator.replay import replay_run

    async with db_session() as session:
        replayed = await replay_run(session, run_id)
        db_tasks = (
            await session.execute(select(Task).where(Task.run_id == run_id))
        ).scalars().all()

    assert replayed.status.value == "completed"
    assert set(replayed.tasks) == {t.key for t in db_tasks}
    for t in db_tasks:
        rp = replayed.tasks[t.key]
        assert rp.status == t.status, f"{t.key}: 事件重建 {rp.status} ≠ 状态表 {t.status}"
        assert rp.title == t.title
    assert replayed.plan_versions and set(replayed.plan_versions) == {1}
    assert len(replayed.attempts) == 2

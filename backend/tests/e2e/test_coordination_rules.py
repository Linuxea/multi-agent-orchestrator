"""协调规则测试（§5.7 规则表逐行）与引擎硬约束。"""

from __future__ import annotations

import pytest

import tests.scriptutil as S
from app.models import Task
from app.orchestrator.engine import WakeState
from app.orchestrator.helpers import db_session
from tests.conftest import Harness

pytestmark = pytest.mark.asyncio


def _default_o_intro(agent: str = "worker") -> list[dict]:
    """规则类剧本的公共开头：提出计划并派发 T1。"""
    return [
        S.step_tools(S.propose_plan(S.task_spec("T1", agent))),
        S.step_tools(S.dispatch("T1", agent)),
        S.step_text(),
    ]


def _ending(lead_text: bool = True) -> list[dict]:
    """公共结尾：复核通过并汇总。

    lead_text=True 用于"上一步是 dispatch"的流程（同一唤醒内派发后需要一次文本收尾）；
    False 用于"上一步已是文本收尾"的流程（自动重试不消耗主 agent 步骤）。
    """
    steps: list[dict] = []
    if lead_text:
        steps.append(S.step_text())
    steps += [
        S.step_tools(S.read_artifact("T1"), S.review_pass("T1")),
        S.step_text(),
        S.step_tools(S.finalize()),
        S.step_text(),
    ]
    return steps


async def _setup_single_worker(platform: Harness, **agent_kwargs: object) -> None:
    budget = agent_kwargs.pop(
        "budget", {"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30}
    )
    await platform.agent("worker", "执行员", budget=budget, **agent_kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# done：标准不满足 → 返工，附具体必改项
# ----------------------------------------------------------------------
async def test_rework_with_required_changes(platform: Harness) -> None:
    h = platform
    await _setup_single_worker(h)
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                S.step_tools(S.review_fail("T1", required_changes=["必须补充第三个来源"])),
                S.step_tools(S.dispatch("T1", "worker", rework_feedback="必须补充第三个来源")),
                *_ending(),
            ),
            S.w_scope(
                "worker",
                S.step_tools(S.report_done("第一版只有 2 个来源")),
                S.step_tools(S.report_done("第二版补齐了三个来源并更新了结论与数据")),
            ),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=30)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "accepted"
    assert tasks["T1"].attempt_count == 2
    types = await h.event_types(run_id)
    assert "review.submitted" in types
    events = await h.events(run_id, ["task.status_changed"])
    statuses = [e.payload["to_status"] for e in events if e.payload.get("key") == "T1"]
    assert "needs_rework" in statuses
    # 返工任务单携带复核意见
    attempts = await h.attempts(tasks["T1"].id)
    assert attempts[1].brief["rework_feedback"] == "必须补充第三个来源"
    assert attempts[1].brief["rework_of"] == attempts[0].id


# ----------------------------------------------------------------------
# partial：采纳必须写明缺口（硬约束：不写被拒绝）
# ----------------------------------------------------------------------
async def test_partial_accept_requires_gaps_note(platform: Harness) -> None:
    h = platform
    await _setup_single_worker(h)
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                # 已读交付物但不写 gaps_note 的采纳 → 被拒绝
                S.step_tools(
                    S.read_artifact("T1"),
                    S.review_pass("T1", verdict="pass_with_gaps"),
                ),
                # 再带 gaps_note → 通过
                S.step_tools(
                    S.review_pass("T1", verdict="pass_with_gaps", gaps_note="缺 1 个来源的数据"),
                ),
                S.step_text(),
                S.step_tools(S.finalize(gaps="T1 缺 1 个来源的数据")),
                S.step_text(),
            ),
            S.w_scope(
                "worker",
                S.step_tools(S.report_partial("完成 2/3 来源的调研")),
                S.step_tools(S.report_partial("依旧缺最后一个来源")),  # 备用
            ),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=30)
    rejected = await h.events(run_id, ["orchestrator.tool_rejected"])
    assert any("gaps_note" in e.payload["reason"] or "缺口" in e.payload["reason"] for e in rejected)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "accepted"
    run = await h.run_row(run_id)
    assert run.outcome and run.outcome.value == "partial"
    assert "缺" in run.summary_of_gaps


# ----------------------------------------------------------------------
# blocked · missing_info → ask_user → 用户回答 → 重派（附新信息）
# ----------------------------------------------------------------------
async def test_missing_info_ask_user_then_redispatch(platform: Harness) -> None:
    h = platform
    await _setup_single_worker(h)
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                S.step_tools(S.ask_user(question="目标市场是国内还是海外？", blocking=["T1"])),
                S.step_text(),
                S.step_tools(S.dispatch("T1", "worker", context="用户已确认：目标市场为国内")),
                *_ending(),
            ),
            S.w_scope(
                "worker",
                S.step_tools(S.report_blocked("missing_info", detail="不知道目标市场")),
                S.step_tools(S.report_done("按国内市场完成调研，三个来源齐备且口径一致")),
            ),
        )
    )
    run_id, _ = await h.start()
    await h.wait(run_id, timeout=10)  # 等待 ask_user 挂起
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "awaiting_user"
    types = await h.event_types(run_id)
    assert "user.question_asked" in types

    await h.answer(run_id, "国内市场")
    assert await h.wait(run_id, timeout=30)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "accepted"
    types = await h.event_types(run_id)
    assert "user.answered" in types
    attempts = await h.attempts(tasks["T1"].id)
    assert attempts[1].brief["context"] == "用户已确认：目标市场为国内"


# ----------------------------------------------------------------------
# blocked · missing_tool / out_of_scope → 改派（不能原样重派给同一 agent）
# ----------------------------------------------------------------------
@pytest.mark.parametrize("blocker", ["missing_tool", "out_of_scope"])
async def test_reassign_for_capability_blockers(platform: Harness, blocker: str) -> None:
    h = platform
    await _setup_single_worker(h)
    await h.agent("helper", "补位执行员", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                # 先原样重派同一 agent → 引擎拒绝（§5.7 硬约束）
                S.step_tools(S.dispatch("T1", "worker")),
                S.step_tools(S.reassign("T1", "helper", reason=f"{blocker}，改派")),
                S.step_tools(S.dispatch("T1", "helper")),
                *_ending(),
            ),
            S.w_scope("worker", S.step_tools(S.report_blocked(blocker, detail="缺少所需工具"))),
            S.w_scope("helper", S.step_tools(S.report_done("补位完成，含三个来源与结论"))),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=30)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "accepted"
    types = await h.event_types(run_id)
    assert "task.reassigned" in types
    rejected = await h.events(run_id, ["orchestrator.tool_rejected"])
    assert any("同一 agent" in e.payload["reason"] for e in rejected)


# ----------------------------------------------------------------------
# blocked · needs_collaboration → revise_plan 显式建立前置依赖
# ----------------------------------------------------------------------
async def test_needs_collaboration_adds_prerequisite(platform: Harness) -> None:
    h = platform
    await _setup_single_worker(h)
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                S.step_tools(
                    S.revise_plan(
                        {
                            "add_tasks": [S.task_spec("T0", "worker", title="修复前置问题")],
                            "update_tasks": [{"key": "T1", "depends_on": ["T0"]}],
                        },
                        reason="T1 需要前置修复",
                    )
                ),
                S.step_tools(S.dispatch("T0", "worker")),
                S.step_text(),
                # T0 完成后：复核 T0 并在同一次唤醒内重派 T1
                S.step_tools(S.read_artifact("T0"), S.review_pass("T0"), S.dispatch("T1", "worker")),
                *_ending(),
            ),
            S.w_scope(
                "worker",
                S.step_tools(S.report_blocked("needs_collaboration", detail="需要先修复数据")),
                S.step_tools(S.report_done("前置问题已修复")),
                S.step_tools(S.report_done("前置就绪后完成主任务，三个来源齐")),
            ),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=40)
    tasks = await h.tasks(run_id)
    assert set(tasks) == {"T0", "T1"}
    assert tasks["T1"].status.value == "accepted"
    assert tasks["T1"].depends_on == ["T0"]
    types = await h.event_types(run_id)
    assert "plan.revised" in types
    run = await h.run_row(run_id)
    assert run.current_plan_version == 2


# ----------------------------------------------------------------------
# blocked · external_error → 引擎自动重试（不计入尝试次数，最多 2 次）
# ----------------------------------------------------------------------
async def test_external_error_auto_retry_not_counted(platform: Harness) -> None:

    h = platform
    await _setup_single_worker(h)
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                *_ending(lead_text=False),
            ),
            S.w_scope(
                "worker",
                S.step_tools(S.report_blocked("external_error", detail="API 500")),
                S.step_tools(S.report_done("外部服务恢复后完成，含三个来源")),
            ),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=30)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "accepted"
    attempts = await h.attempts(tasks["T1"].id)
    assert len(attempts) == 2
    assert attempts[0].report["blocker"]["type"] == "external_error"
    assert attempts[1].auto_retry_of == attempts[0].id
    assert attempts[1].counted is False
    assert tasks["T1"].attempt_count == 1, "自动重试不计入尝试次数"


# ----------------------------------------------------------------------
# blocked · budget_exhausted → 采纳部分结果并写入 gaps
# ----------------------------------------------------------------------
async def test_budget_exhausted_accept_partial(platform: Harness) -> None:
    h = platform
    # max_turns=1：第一轮后运行时自动生成 budget_exhausted 回报（§5.4）
    await _setup_single_worker(h, budget={"max_turns": 1, "max_cost": 0.1, "max_wall_seconds": 30})
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                S.step_tools(
                    S.review_pass("T1", verdict="pass_with_gaps", gaps_note="预算耗尽只完成部分")
                ),
                S.step_text(),
                S.step_tools(S.finalize(gaps="T1 因预算耗尽只完成部分")),
                S.step_text(),
            ),
            S.w_scope("worker", S.step_text("正在整理……")),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=30)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "accepted"
    attempts = await h.attempts(tasks["T1"].id)
    assert attempts[0].report["blocker"]["type"] == "budget_exhausted"
    run = await h.run_row(run_id)
    assert run.outcome and run.outcome.value == "partial"


# ----------------------------------------------------------------------
# failed → 复核确认不可完成 → failed 并写入 gaps
# ----------------------------------------------------------------------
async def test_failed_report_marks_task_failed(platform: Harness) -> None:
    h = platform
    await _setup_single_worker(h)
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                S.step_tools(S.review_fail("T1", required_changes=["无法通过返工解决"])),
                S.step_text(),
                S.step_tools(S.finalize(gaps="T1 确认无法完成")),
                S.step_text(),
            ),
            S.w_scope("worker", S.step_tools(S.report_failed("数据源不存在"))),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=30)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "failed"
    assert tasks["T1"].attempt_count == 1
    run = await h.run_row(run_id)
    # 没有任何任务被采纳 → outcome=failed（partial 保留给"部分完成"）
    assert run.outcome and run.outcome.value == "failed"
    assert "T1" in run.summary_of_gaps


# ----------------------------------------------------------------------
# 崩溃 → 引擎自动重试一次（同 agent，新会话，不计入尝试次数）
# ----------------------------------------------------------------------
async def test_crash_auto_retry_once(platform: Harness) -> None:
    h = platform
    await _setup_single_worker(h)
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                *_ending(lead_text=False),
            ),
            S.w_scope(
                "worker",
                # 持续 5xx：repeat 覆盖引擎的 3 次重试 → ModelError → 尝试崩溃
                {"error": {"status_code": 500}, "repeat": 4},
                S.step_tools(S.report_done("新会话重试后完成，含三个来源")),
            ),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=30)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "accepted"
    attempts = await h.attempts(tasks["T1"].id)
    assert len(attempts) == 2
    assert attempts[0].status.value == "crashed"
    assert attempts[1].counted is False
    assert tasks["T1"].attempt_count == 1
    types = await h.event_types(run_id)
    assert "attempt.crashed" in types


# ----------------------------------------------------------------------
# 同一任务改派达 2 次 → 上报用户（第三次改派被引擎拒绝）
# ----------------------------------------------------------------------
async def test_reassign_limit_escalates_to_user(platform: Harness) -> None:
    h = platform
    await h.agent("a1", "执行员一", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    await h.agent("a2", "执行员二", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    await h.agent("a3", "执行员三", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                S.step_tools(S.propose_plan(S.task_spec("T1", "a1"))),
                S.step_tools(S.dispatch("T1", "a1")),
                S.step_text(),
                S.step_tools(S.reassign("T1", "a2", reason="缺工具")),
                S.step_tools(S.dispatch("T1", "a2")),
                S.step_text(),
                S.step_tools(S.reassign("T1", "a3", reason="仍缺工具")),
                S.step_tools(S.dispatch("T1", "a3")),
                S.step_text(),
                # 第三次改派应被拒绝 → 改为 ask_user 上报
                S.step_tools(S.reassign("T1", "a1", reason="再试一次")),
                S.step_tools(S.ask_user(question="T1 多次受阻，如何处理？", blocking=["T1"])),
                S.step_text(),
                S.step_tools(S.dispatch("T1", "a3", context="用户建议继续")),
                *_ending(),
            ),
            S.w_scope("a1", S.step_tools(S.report_blocked("missing_tool", detail="缺工具"))),
            S.w_scope("a2", S.step_tools(S.report_blocked("missing_tool", detail="缺工具"))),
            S.w_scope(
                "a3",
                S.step_tools(S.report_blocked("needs_decision", detail="需要用户决定")),
                S.step_tools(S.report_done("按用户决定完成，含三个来源")),
            ),
        )
    )
    run_id, _ = await h.start()
    await h.wait(run_id, timeout=10)
    rejected = await h.events(run_id, ["orchestrator.tool_rejected"])
    assert any("改派" in e.payload["reason"] for e in rejected)
    # 用户手动追加尝试次数（§5.7：达到 max_attempts 后用户可追加）
    tasks_before = await h.tasks(run_id)
    async with db_session() as session:
        t = await session.get(Task, tasks_before["T1"].id)
        assert t is not None
        t.max_attempts += 2
    await h.answer(run_id, "继续用 a3")
    assert await h.wait(run_id, timeout=40)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "accepted"


# ----------------------------------------------------------------------
# 复核硬约束：未读取交付物直接 pass 被拒绝（§5.9）
# ----------------------------------------------------------------------
async def test_review_requires_read_artifact(platform: Harness) -> None:
    h = platform
    await _setup_single_worker(h)
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                # 未读交付物直接 pass → 拒绝
                S.step_tools(S.review_pass("T1")),
                # 读取后复核 → 通过
                S.step_tools(S.read_artifact("T1"), S.review_pass("T1")),
                S.step_text(),
                S.step_tools(S.finalize()),
                S.step_text(),
            ),
            S.w_scope(
                "worker",
                S.step_tools(S.report_done("完成，含三个来源")),
                S.step_tools(S.report_done("备用")),  # 不应被消费
            ),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=30)
    rejected = await h.events(run_id, ["orchestrator.tool_rejected"])
    assert any("read_artifact" in e.payload["reason"] for e in rejected)
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status.value == "accepted"


# ----------------------------------------------------------------------
# 死循环防护：返工内容高度相似 → 阻止第三次返工并上报（§11.3）
# ----------------------------------------------------------------------
async def test_deadloop_guard_blocks_similar_rework(platform: Harness) -> None:
    h = platform
    await _setup_single_worker(h)
    same_summary = "完全相同的回报内容，两次一字不差"
    h.scopes(
        S.load_scopes_dict(
            S.o_scope(
                *_default_o_intro(),
                S.step_tools(S.review_fail("T1")),
                S.step_tools(S.dispatch("T1", "worker", rework_feedback="修改后重交")),
                S.step_text(),
                S.step_tools(S.review_fail("T1")),
                # 相似度 >90% → 第三次派发被拒绝
                S.step_tools(S.dispatch("T1", "worker", rework_feedback="再次修改")),
                # needs_rework 状态不可被提问阻塞（D-1.3）→ 非阻塞式上报
                S.step_tools(S.ask_user(question="检测到重复返工，如何处理？")),
                S.step_text(),
            ),
            S.w_scope(
                "worker",
                S.step_tools(S.report_done(same_summary)),
                S.step_tools(S.report_done(same_summary)),
                S.step_tools(S.report_done(same_summary)),
            ),
        )
    )
    run_id, _ = await h.start()
    assert await h.wait(run_id, timeout=30)
    rejected = await h.events(run_id, ["orchestrator.tool_rejected"])
    assert any("高度相似" in e.payload["reason"] for e in rejected)
    types = await h.event_types(run_id)
    assert "user.question_asked" in types


# ----------------------------------------------------------------------
# 跳过硬约束：关键任务（被下游依赖）不可直接 skip（§5.2）
# ----------------------------------------------------------------------
async def test_skip_critical_task_rejected(platform: Harness) -> None:
    h = platform
    await _setup_single_worker(h)
    run_id, _ = await h.start()
    ws = WakeState(run_id=run_id)
    engine = h.container.engine
    await engine._execute_control_tool(
        ws, "propose_plan",
        {
            "tasks": [
                S.task_spec("T1", "worker"),
                S.task_spec("T2", "worker", depends_on=["T1"]),
            ],
            "rationale": "测试",
        },
    )
    result = await engine._execute_control_tool(
        ws, "skip", {"task_key": "T1", "rationale": "想跳过关键任务"}
    )
    assert "error" in result and "关键任务" in result["error"]

    # 无下游依赖的任务可以跳过
    result2 = await engine._execute_control_tool(
        ws, "skip", {"task_key": "T2", "rationale": "跳过无依赖任务"}
    )
    assert result2.get("ok") is True

"""真实端点编排全链路（L1）：完整业务闭环。

覆盖：多 agent 计划 → 派发 → 真实 worker 执行 → 复核 → 终答（用户核心场景）、
计划驳回修订循环、多轮澄清推进。每条链路的模型调用全部走真实端点。

运行：
    DEEPSEEK_API_KEY=sk-... uv run pytest tests/e2e/test_real_full_pipeline.py -m real_model
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.enums import RunOutcome, RunStatus, TaskStatus
from tests.conftest import eventually

pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(
        not __import__("os").environ.get("DEEPSEEK_API_KEY"),
        reason="未设置 DEEPSEEK_API_KEY",
    ),
]

RESEARCH_REQUEST = (
    "请安排一次小调研：让 researcher 用一句话回答「2026 年最流行的编程语言是什么」，"
    "再让 writer 把这个结论扩写成两句话的简报。writer 依赖 researcher 的结果。"
    "全程不需要使用任何外部工具，凭模型知识完成即可。"
)


async def _seed_agents(p: Any) -> None:
    await p.agent(
        "researcher", "调研员",
        prompt="你是调研员。根据已有知识用一句话直接回答问题，然后立即用 submit_report 提交 done 回报。不要使用其他工具。",
    )
    await p.agent(
        "writer", "撰稿人",
        prompt="你是撰稿人。基于任务单给出的背景结论写两句话简报，然后立即用 submit_report 提交 done 回报。不要使用其他工具。",
    )


async def _drive_to_terminal(p: Any, run_id: str, max_answers: int, answer: str) -> str:
    """应答循环：ask_user 出现即答；返回终止原因（terminal / answers_exhausted）。"""
    answered = 0
    for _ in range(int(300 / 0.3)):
        await asyncio.sleep(0.3)
        row = await p.run_row(run_id)
        if row.status.is_terminal:
            return "terminal"
        qs = p.container.engine.open_questions(run_id)
        if qs:
            assert answered < max_answers, f"应答超过 {max_answers} 轮仍未推进（交互传达缺陷）"
            answered += 1
            await p.container.engine.submit_answer(run_id, answer, question_id=qs[-1].question_id)
    return "timeout"


async def test_full_research_pipeline(real_platform: Any) -> None:
    """完整调研链路：计划→批准→派发→真实执行→复核→终答（用户核心场景回归）。"""
    p = real_platform
    await _seed_agents(p)
    await p.patch_settings({"plan_approval_mode": "never"})

    run_id, _sess = await p.start(RESEARCH_REQUEST)
    why = await _drive_to_terminal(p, run_id, max_answers=2, answer="按任务单直接完成，无需补充信息")
    assert why == "terminal", f"应达终态，实际 {why}"

    row = await p.run_row(run_id)
    types = await p.event_types(run_id)
    tasks = await p.tasks(run_id)

    assert row.status == RunStatus.completed, f"终态 completed，实际 {row.status.value}\n{await p.debug_events(run_id)}"
    # 推测性调研问题模型可能如实申报 gaps → partial 也是健康终态；关键是链路完整
    assert row.outcome in (RunOutcome.success, RunOutcome.partial), (
        f"outcome 异常：{row.outcome}\n{await p.debug_events(run_id)}"
    )
    assert (row.final_answer or "").strip()
    # 编排主链路事件
    for expected in ("plan.proposed", "plan.approved", "attempt.started", "attempt.reported", "run.completed"):
        assert expected in types, f"缺少事件 {expected}\n{await p.debug_events(run_id)}"
    # 任务全部被采纳（≥1 个任务）
    assert tasks, "应有任务"
    assert all(t.status == TaskStatus.accepted for t in tasks.values()), (
        {k: v.status.value for k, v in tasks.items()}
    )
    # 会话恢复锚点落库（第五期：attempt.executor_session_id）
    for t in tasks.values():
        ats = await p.attempts(t.id)
        assert ats, f"任务 {t.key} 应有尝试"


async def test_plan_reject_then_revise(real_platform: Any) -> None:
    """计划驳回循环：驳回理由必须被模型理解并驱动 v2 修订。"""
    p = real_platform
    await _seed_agents(p)
    await p.patch_settings({"plan_approval_mode": "always"})

    run_id, _sess = await p.start(RESEARCH_REQUEST)

    # 等待 v1 计划进入待审批
    async def _awaiting() -> Any:
        row = await p.run_row(run_id)
        return row.status.value == "awaiting_plan_approval"

    await eventually(_awaiting, timeout=180, msg="计划应进入待审批")

    engine = p.container.engine
    await engine.reject_plan(run_id, 1, "计划太重了：去掉 writer 任务，只保留 researcher 一句话回答")

    # 驳回理由必须驱动模型修订（revise_plan 出 v2、writer 任务被移除）；
    # 引擎语义：修订版不再重复进入待审批（审批拦截仅首次提案）
    async def _v2_revised() -> Any:
        types = await p.event_types(run_id)
        row = await p.run_row(run_id)
        return "plan.revised" in types and (row.current_plan_version or 0) >= 2

    try:
        await eventually(_v2_revised, timeout=180, msg="驳回后应修订出 v2")
    except AssertionError:
        pytest.fail(f"驳回后未产出 v2：\n{await p.debug_events(run_id)}")

    why = await _drive_to_terminal(p, run_id, max_answers=2, answer="直接完成")
    assert why == "terminal", f"驳回修订后未到终态：\n{await p.debug_events(run_id)}"
    row = await p.run_row(run_id)
    assert row.status == RunStatus.completed, f"{await p.debug_events(run_id)}"
    tasks = await p.tasks(run_id)
    live = {k: v for k, v in tasks.items() if v.status != TaskStatus.cancelled}
    assert len(live) == 1, f"驳回理由要求砍到 1 个任务，实际 {list(live)}：\n{await p.debug_events(run_id)}"


async def test_multi_clarification_progresses(real_platform: Any) -> None:
    """多轮澄清：模糊需求最多 2 轮问答后必须开始行动（防无限澄清空转）。"""
    p = real_platform
    await _seed_agents(p)
    await p.patch_settings({"plan_approval_mode": "never"})

    run_id, _sess = await p.start("帮我弄点东西")
    why = await _drive_to_terminal(
        p, run_id, max_answers=2,
        answer="就做一件事：让 researcher 用一句话介绍 Python，然后直接终答。",
    )
    assert why == "terminal", "2 轮澄清后应能推进到终态"
    row = await p.run_row(run_id)
    assert row.status == RunStatus.completed, f"{await p.debug_events(run_id)}"
    assert (row.final_answer or "").strip()

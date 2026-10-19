"""用户输入可见性测试：补充消息 / ask_user 回答 / 计划驳回理由必须进入主 agent 上下文。

背景（缺陷）：此前这三类用户输入只产生事件与唤醒原因，digest 仅回读触发需求，
主 agent 永远看不到内容——表现为「交互不自然、信息传达不到 AI」。
修复：统一落为 user 角色 Message，digest 按时间线呈现（见 engine/context）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.models import Message, Run, Session
from app.orchestrator.context import build_run_digest
from app.orchestrator.engine import PendingQuestion


async def _digest_for_run(engine: Any, run_id: str) -> str:
    from app.orchestrator.helpers import db_session

    async with db_session() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        tasks = await engine._run_tasks(session, run_id)
        agents = await engine.registry.list_agents(session)
        return await build_run_digest(session, run, tasks, {a.id: a for a in agents})


async def test_supplement_message_reaches_digest(env: Any) -> None:
    """run 进行中的补充消息必须出现在运行状态摘要里。"""
    from app.orchestrator.helpers import db_session

    engine = env.engine
    async with db_session() as s:
        sess = Session(id="sess_t1", title="t")
        s.add(sess)
        await s.flush()

    _msg, run = await engine.submit_user_message("sess_t1", "调研 AI 编程工具市场")
    _msg2, run2 = await engine.submit_user_message("sess_t1", "重点看国内厂商，忽略海外")
    assert run2.id == run.id, "活跃 run 期间的补充应挂在同一 run"

    digest = await _digest_for_run(engine, run.id)
    assert "重点看国内厂商，忽略海外" in digest, "补充消息内容必须进入 digest"
    assert "[本次运行中的新输入]" in digest
    assert "[本次需求] 调研 AI 编程工具市场" in digest


async def test_ask_user_answer_reaches_digest(env: Any) -> None:
    """ask_user 的回答（含问题原文）必须落库并进入 digest。"""
    from app.orchestrator.helpers import db_session

    engine = env.engine
    async with db_session() as s:
        sess = Session(id="sess_t2", title="t")
        s.add(sess)
        await s.flush()

    _msg, run = await engine.submit_user_message("sess_t2", "写一份竞品简报")
    engine._questions[run.id] = {
        "q1": PendingQuestion(
            question_id="q1",
            question="目标读者是谁？",
            options=["技术团队", "管理层"],
            blocking_task_keys=[],
        )
    }
    await engine.submit_answer(run.id, "管理层", question_id="q1")

    digest = await _digest_for_run(engine, run.id)
    assert "【用户回答】" in digest
    assert "问：目标读者是谁？" in digest
    assert "答：管理层" in digest


async def test_answer_without_question_still_persisted(env: Any) -> None:
    """无匹配待答问题时，回答也不应静默丢弃。"""
    from app.orchestrator.helpers import db_session

    engine = env.engine
    async with db_session() as s:
        sess = Session(id="sess_t3", title="t")
        s.add(sess)
        await s.flush()

    _msg, run = await engine.submit_user_message("sess_t3", "开始")
    await engine.submit_answer(run.id, "自由发挥")

    async with db_session() as s:
        msgs = (
            (await s.execute(select(Message).where(Message.run_id == run.id, Message.role == "user")))
            .scalars()
            .all()
        )
    assert any("答：自由发挥" in m.content for m in msgs), "游离回答也应落库可见"


async def test_plan_rejection_note_reaches_digest(env: Any) -> None:
    """计划驳回理由必须落库并进入 digest。"""
    from app.orchestrator.helpers import db_session

    engine = env.engine
    async with db_session() as s:
        sess = Session(id="sess_t4", title="t")
        s.add(sess)
        await s.flush()

    _msg, run = await engine.submit_user_message("sess_t4", "做个方案")
    await engine.reject_plan(run.id, 1, "预算超了，砍掉市场调研任务")

    digest = await _digest_for_run(engine, run.id)
    assert "【计划被驳回】v1" in digest
    assert "砍掉市场调研任务" in digest


async def test_previous_run_final_answer_visible_to_new_run(env: Any) -> None:
    """追问场景：新 run 应看到上一 run 的结论与会话早前输入。"""
    from app.enums import RunStatus
    from app.orchestrator.helpers import db_session

    engine = env.engine
    async with db_session() as s:
        sess = Session(id="sess_t5", title="t")
        s.add(sess)
        await s.flush()

    msg1, run1 = await engine.submit_user_message("sess_t5", "第一轮：调研市场")
    async with db_session() as s:
        r1 = (await s.execute(select(Run).where(Run.id == run1.id))).scalar_one()
        r1.status = RunStatus.completed
        r1.final_answer = "市场结论 ABC"
        await s.flush()

    _msg2, run2 = await engine.submit_user_message("sess_t5", "基于刚才的结论深挖竞品")
    digest = await _digest_for_run(engine, run2.id)
    assert "市场结论 ABC" in digest, "上一 run 结论应进入新 run 摘要"
    assert "[会话早前输入]" in digest

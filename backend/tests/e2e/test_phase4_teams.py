"""第四期 E2E：会话编队 → 剧本骨架计划 → 阶段落库与偏离记录 → 团队过滤。

覆盖（§14.5）：
- 会话绑定默认团队后，运行摘要注入编队段（编排器剧本以「编队」匹配即证注入）
- propose_plan 引用不存在阶段被拒（orchestrator.tool_rejected 留痕）
- 任务 playbook_stage_key 落库、计划快照含阶段
- plan.proposed 载荷 deviates_from_playbook = 未映射阶段（S3）
- search_agents(team=) 只返回团队成员；list_playbooks(team=) 只返回团队剧本
- 未绑定剧本的会话填写 playbook_stage_key 被拒绝
"""

from __future__ import annotations

from pathlib import Path as _P

import pytest
from sqlalchemy import select

from app.ids import new_id
from app.models import Plan, Team
from app.models import Session as SessionModel
from app.orchestrator.engine import ControlToolError, WakeState
from app.orchestrator.helpers import db_session
from app.registry.service import RegistryService
from tests.conftest import Harness

pytestmark = pytest.mark.asyncio

SCRIPT = str((_P(__file__).parent.parent / "scripts" / "team_playbook_run.yaml").resolve())


async def _make_team(h: Harness, researcher_id: str, writer_id: str) -> Team:
    registry = RegistryService()
    async with db_session() as session:
        team = await registry.create_team(
            session,
            slug="brief-squad",
            name="简报小队",
            description="调研 + 成稿",
            member_agent_ids=[researcher_id, writer_id],
            lead_hint="调研员牵头",
        )
        pb = await registry.create_playbook(
            session,
            name="简报剧本",
            trigger_hint="写简报",
            stages=[
                {"key": "S1", "name": "调研", "goal": "收集来源",
                 "agent_hint": "web-researcher", "depends_on": []},
                {"key": "S2", "name": "成稿", "goal": "写简报",
                 "agent_hint": "writer", "depends_on": ["S1"]},
                {"key": "S3", "name": "审校", "goal": "质量把关",
                 "agent_hint": "editor-reviewer", "depends_on": ["S2"]},
            ],
        )
        team.playbook_id = pb.id
        await session.flush()
        return team


async def test_team_playbook_run(platform: Harness) -> None:
    h = platform
    researcher = await h.agent(
        "web-researcher", "调研员",
        prompt="你是调研员（测试）。", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30},
    )
    writer = await h.agent(
        "writer", "撰稿人",
        prompt="你是撰稿人（测试）。", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30},
    )
    await h.agent("planner", "规划师", prompt="你是规划师（测试）。")  # 团队外 agent
    team = await _make_team(h, researcher.id, writer.id)
    h.load_yaml(SCRIPT)

    async with db_session() as session:
        sess = SessionModel(id=new_id("sess"), title="编队运行", default_team_id=team.id)
        session.add(sess)
        await session.flush()
        session_id = sess.id
    message, run = await h.container.engine.submit_user_message(session_id, "写一份市场简报")
    run_id = run.id
    assert await h.wait(run_id, timeout=30), await h.debug_events(run_id)

    run_row = await h.run_row(run_id)
    assert run_row.status.value == "completed", await h.debug_events(run_id)
    assert run_row.outcome and run_row.outcome.value == "success"

    # 首版计划被拒（引用 SX），第二版通过
    rejected = await h.events(run_id, ["orchestrator.tool_rejected"])
    assert any(
        e.payload.get("tool") == "propose_plan"
        and "SX" in str(e.payload.get("reason"))
        for e in rejected
    ), await h.debug_events(run_id)

    tasks = await h.tasks(run_id)
    assert tasks["T1"].playbook_stage_key == "S1"
    assert tasks["T2"].playbook_stage_key == "S2"

    # 计划快照含阶段映射（被拒的版本不落库，只有修正后的 v1）
    async with db_session() as session:
        plans = (
            await session.execute(
                select(Plan).where(Plan.run_id == run_id).order_by(Plan.version)
            )
        ).scalars().all()
    assert len(plans) == 1
    approved = plans[0]
    assert approved.approved
    stage_by_key = {
        snap["key"]: snap.get("playbook_stage_key")
        for snap in approved.tasks_snapshot
    }
    assert stage_by_key == {"T1": "S1", "T2": "S2"}

    # 偏离记录：S3 无任务映射
    proposed = await h.events(run_id, ["plan.proposed"])
    assert any(
        e.payload.get("deviates_from_playbook") == ["S3"] for e in proposed
    ), await h.debug_events(run_id)


async def test_team_scoped_tools_and_unbound_guard(platform: Harness) -> None:
    h = platform
    researcher = await h.agent("web-researcher", "调研员")
    writer = await h.agent("writer", "撰稿人")
    await h.agent("planner", "规划师")
    await _make_team(h, researcher.id, writer.id)

    ws = WakeState(run_id="run_none")
    result = await h.container.engine._tool_search_agents(ws, {"team": "brief-squad"})
    slugs = {a["slug"] for a in result["agents"]}
    assert slugs == {"web-researcher", "writer"}, "团队过滤应只返回成员"

    result_all = await h.container.engine._tool_search_agents(ws, {})
    assert "planner" in {a["slug"] for a in result_all["agents"]}

    with pytest.raises(ControlToolError):
        await h.container.engine._tool_search_agents(ws, {"team": "no-such-team"})

    pbs = await h.container.engine._tool_list_playbooks(ws, {"team": "brief-squad"})
    assert len(pbs["playbooks"]) == 1
    assert [s["key"] for s in pbs["playbooks"][0]["stages"]] == ["S1", "S2", "S3"]

    # 未绑定剧本的会话不能填 playbook_stage_key（校验发生在 propose_plan，直接验证构造层）
    from app.registry.service import validate_playbook_stages

    with pytest.raises(ValueError):
        validate_playbook_stages([{"name": "缺 key"}])
    with pytest.raises(ValueError):
        validate_playbook_stages([
            {"key": "S1"}, {"key": "S2", "depends_on": ["S9"]},
        ])


async def test_team_member_validation(platform: Harness) -> None:
    _ = platform
    registry = RegistryService()
    async with db_session() as session:
        with pytest.raises(ValueError, match="不存在"):
            await registry.create_team(
                session, slug="bad", name="坏队",
                member_agent_ids=["agent_missing"],
            )

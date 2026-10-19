"""Agent 工厂单元测试（§14.5）：起草解析、成员过滤、事件留痕。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.factory.service import FactoryError, _extract_json
from app.models import Event
from app.orchestrator.helpers import db_session
from tests.conftest import Harness

pytestmark = pytest.mark.asyncio

AGENT_JSON = (
    '{"slug": "market-scout", "name": "市场侦察", '
    '"description": "快速扫描市场格局并产出要点", '
    '"role_detail": "擅长：桌面调研。\\n不做：付费数据。", '
    '"capability_tags": ["research", "market"], '
    '"system_prompt": "你是市场侦察员。", '
    '"input_contract": "一句需求", "output_contract": "Markdown 要点", '
    '"default_budget": {"max_turns": 20, "max_cost": 0.5, "max_wall_seconds": 900}, '
    '"avatar": "🛰️"}'
)

TEAM_JSON = (
    '{"slug": "squad", "name": "简报小队", "description": "调研加成稿", '
    '"member_slugs": ["web-researcher", "writer", "ghost-agent"], '
    '"lead_hint": "调研员牵头", "missing_capabilities": ["设计"], '
    '"playbook": {"name": "简报剧本", "trigger_hint": "写简报", "stages": ['
    '{"key": "S1", "name": "调研", "goal": "收集来源", "agent_hint": "web-researcher", "depends_on": []}, '
    '{"key": "S2", "name": "成稿", "goal": "写简报", "agent_hint": "writer", "depends_on": ["S1"]}]}}'
)


async def test_extract_json_tolerates_fences() -> None:
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('好的，如下：\n{"a": {"b": 2}} 请查收') == {"a": {"b": 2}}
    with pytest.raises(FactoryError):
        _extract_json("没有 JSON")


async def test_draft_agent_happy_path(platform: Harness) -> None:
    h = platform
    h.scopes(
        {
            "scopes": [
                {"match": {"system_contains": "Agent 工厂"}, "steps": [{"text": AGENT_JSON}]},
                {"match": {"system_contains": "团队工厂"}, "steps": [{"text": TEAM_JSON}]},
            ]
        }
    )
    async with db_session() as session:
        draft = await h.container.factory.draft_agent(session, "需要一个市场侦察 agent")
        assert draft["slug"] == "market-scout"
        assert draft["name"] == "市场侦察"
        assert draft["slug_available"] is True
        assert draft["default_budget"] == {
            "max_turns": 20, "max_cost": 0.5, "max_wall_seconds": 900,
        }
        assert draft["executor"] == "lite"

        # 事件留痕：全局通道（run_id 为空）、kind=agent_draft
        evs = (
            await session.execute(
                select(Event).where(Event.type == "factory.generated")
            )
        ).scalars().all()
        assert len(evs) == 1
        assert evs[0].run_id is None
        assert evs[0].payload["kind"] == "agent_draft"
        assert "市场侦察" in evs[0].payload["output_summary"]


async def test_draft_agent_slug_conflict(platform: Harness) -> None:
    h = platform
    h.scopes(
        {"scopes": [{"match": {"system_contains": "Agent 工厂"}, "steps": [{"text": AGENT_JSON}]}]}
    )
    await h.agent("market-scout", "已占用")
    h.scopes(
        {"scopes": [{"match": {"system_contains": "Agent 工厂"}, "steps": [{"text": AGENT_JSON}]}]}
    )
    async with db_session() as session:
        draft = await h.container.factory.draft_agent(session, "再来一个市场侦察")
        assert draft["slug_available"] is False
        assert draft["slug_suggestion"] == "market-scout-2"


async def test_draft_agent_bad_json(platform: Harness) -> None:
    h = platform
    h.scopes(
        {"scopes": [{"match": {"system_contains": "Agent 工厂"}, "steps": [{"text": "我不会输出 JSON"}]}]}
    )
    async with db_session() as session:
        with pytest.raises(FactoryError):
            await h.container.factory.draft_agent(session, "随便")


async def test_draft_team_filters_members(platform: Harness) -> None:
    h = platform
    researcher = await h.agent("web-researcher", "调研员")
    writer = await h.agent("writer", "撰稿人")
    h.scopes(
        {"scopes": [{"match": {"system_contains": "团队工厂"}, "steps": [{"text": TEAM_JSON}]}]}
    )
    async with db_session() as session:
        draft = await h.container.factory.draft_team(session, "组一个写简报的团队")
        # 花名册外的 ghost-agent 被剔除并记录
        assert draft["member_agent_ids"] == [researcher.id, writer.id]
        assert draft["dropped_member_slugs"] == ["ghost-agent"]
        assert draft["missing_capabilities"] == ["设计"]
        stages = draft["playbook"]["stages"]
        assert [s["key"] for s in stages] == ["S1", "S2"]
        assert stages[1]["depends_on"] == ["S1"]
        assert draft["lead_hint"] == "调研员牵头"

        evs = (
            await session.execute(
                select(Event).where(Event.type == "factory.generated")
            )
        ).scalars().all()
        assert evs and evs[0].payload["kind"] == "team_draft"


async def test_draft_team_invalid_stages(platform: Harness) -> None:
    h = platform
    bad = TEAM_JSON.replace('"key": "S2"', '"name": "S2"')  # S2 缺 key
    h.scopes(
        {"scopes": [{"match": {"system_contains": "团队工厂"}, "steps": [{"text": bad}]}]}
    )
    async with db_session() as session:
        with pytest.raises(FactoryError, match="阶段"):
            await h.container.factory.draft_team(session, "组队")

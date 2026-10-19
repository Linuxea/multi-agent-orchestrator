"""真实端点工厂起草（L3）：真实模型生成 agent/team 定义草稿。

运行：
    DEEPSEEK_API_KEY=sk-... uv run pytest tests/e2e/test_real_factory.py -m real_model
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.factory.service import FactoryService

pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"), reason="未设置 DEEPSEEK_API_KEY"
    ),
]


async def test_draft_agent_real_model(real_platform: Any) -> None:
    """真实模型起草 agent：结构完整、可直接经注册表落库。"""
    p = real_platform
    async def _draft() -> Any:
        from app.orchestrator.helpers import db_session

        async with db_session() as session:
            return await FactoryService(
                p.container.events, p.container.registry, p.container.settings
            ).draft_agent(session, "一个擅长把杂乱 CSV 清洗成规范表格的数据整理助手")

    draft = await _draft()
    assert draft["slug"] and draft["name"], f"草稿缺少基本字段：{draft}"
    assert draft["system_prompt"].strip(), "草稿应有系统提示词"
    assert draft["description"].strip()
    assert draft["slug_available"] in (True, False)
    # 语义质量（宽松）：提示词或描述应体现「数据/清洗/表格」相关职责
    blob = draft["system_prompt"] + draft["description"] + draft["role_detail"]
    assert any(k in blob for k in ("数据", "清洗", "表格", "CSV", "csv")), f"草稿语义偏离需求：{blob[:200]}"

    # 草稿可真实落库（工厂契约：人审后保存）
    agent = await p.agent(
        draft["slug"], draft["name"], prompt=draft["system_prompt"],
    )
    assert agent.slug == draft["slug"]


async def test_draft_team_real_model(real_platform: Any) -> None:
    """真实模型起草团队编队：成员选自现有花名册、剧本阶段结构合法。"""
    p = real_platform
    await p.agent("researcher", "调研员", prompt="你是调研员。")
    await p.agent("writer", "撰稿人", prompt="你是撰稿人。")

    from app.orchestrator.helpers import db_session

    async with db_session() as session:
        draft = await FactoryService(
            p.container.events, p.container.registry, p.container.settings
        ).draft_team(session, "一个小型内容生产团队：先调研再成文")
    assert draft.get("name") or draft.get("team", {}).get("name"), f"团队草稿缺少名称：{draft}"
    slugs = set(draft.get("member_slugs") or [])
    if not slugs:
        members = draft.get("members") or draft.get("team", {}).get("members") or []
        slugs = {m.get("slug") or m.get("agent_slug") for m in members if isinstance(m, dict)}
    assert slugs & {"researcher", "writer"}, f"成员应选自现有花名册：{slugs}"

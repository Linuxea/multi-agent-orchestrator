"""第五期 E2E：openai_chat 协议全链路（运行 + 模型体检）走模拟模型 /v1/chat/completions。"""

from __future__ import annotations

from pathlib import Path as _P
from typing import Any

import pytest

from app.enums import ModelProtocol
from app.ids import new_id
from app.models import ModelProfile
from app.orchestrator.helpers import db_session
from tests.conftest import Harness

pytestmark = pytest.mark.asyncio

SCRIPT = str((_P(__file__).parent.parent / "scripts" / "multi_domain_research.yaml").resolve())


async def _openai_profile(h: Harness) -> str:
    async with db_session() as session:
        profile = ModelProfile(
            id=new_id("mp"),
            name="模拟模型（OpenAI 兼容）",
            protocol=ModelProtocol.openai_chat,
            base_url=h.model_url + "/v1",
            model="mock-model",
            max_output_tokens=4096,
            context_window=100000,
        )
        session.add(profile)
        await session.flush()
        await h.container.settings.patch(
            session,
            {
                "orchestrator_model_profile_id": profile.id,
                "worker_default_model_profile_id": profile.id,
            },
        )
        profile_id = profile.id
    h._profile_id = profile_id
    return profile_id


async def test_full_run_over_openai_protocol(platform: Harness) -> None:
    h = platform
    await _openai_profile(h)
    await h.agent(
        "web-researcher", "市场调研员", prompt="你是网络调研员（测试）。",
        budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30},
    )
    await h.agent(
        "writer", "撰稿人", prompt="你是撰稿人（测试）。",
        budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30},
    )
    h.load_yaml(SCRIPT)

    run_id, _session_id = await h.start("调研并写一份简报")
    assert await h.wait(run_id, timeout=30), "运行未在时限内静止"

    run = await h.run_row(run_id)
    assert run.status.value == "completed", await h.debug_events(run_id)
    assert run.outcome and run.outcome.value == "success"
    # 用量经 openai usage 映射累计
    assert run.usage and run.usage.get("input_tokens", 0) > 0

    # 全程请求都打在 /v1/chat/completions：模拟模型剧本消费计数与运行事件一致
    assert h.model_state.request_count > 0
    types = await h.event_types(run_id)
    assert "plan.proposed" in types
    assert "attempt.reported" in types
    assert "run.completed" in types


async def test_model_check_route_over_openai(platform: Harness) -> None:
    """体检路由（§9.6）四项检查对 openai_chat 配置同样成立。"""
    h = platform
    profile_id = await _openai_profile(h)
    h.scopes(
        {
            "scopes": [
                {
                    "match": {"system_contains": "测试助手"},
                    "steps": [
                        {"text": "OK"},
                        {"text": "1 2 3 4 5"},
                        {
                            "tool_calls": [{"name": "echo", "input": {"text": "hello"}}],
                            "stop_reason": "tool_use",
                        },
                    ],
                }
            ]
        }
    )

    from app.api.routes.model_profiles import check_profile

    result: Any = await check_profile(profile_id)
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["基础对话"]["passed"], by_name
    assert by_name["流式输出"]["passed"], by_name
    assert by_name["工具调用"]["passed"], by_name
    assert by_name["用量返回"]["passed"], by_name
    assert result["ok"] is True

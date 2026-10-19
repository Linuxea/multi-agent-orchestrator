"""真实模型冒烟测试（§14.1）：仅在设置 DEEPSEEK_API_KEY 时运行，默认跳过。

运行：DEEPSEEK_API_KEY=sk-... uv run pytest -m real_model
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"), reason="未设置 DEEPSEEK_API_KEY，跳过真实模型冒烟测试"
    ),
]

BASE_URL = "https://api.deepseek.com/anthropic"


@pytest.fixture
async def deepseek_model(env: None) -> object:
    from app.runtime.types import ModelConfig

    return ModelConfig(
        profile_id="mp-deepseek",
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", BASE_URL),
        api_key=os.environ["DEEPSEEK_API_KEY"],
        max_output_tokens=1024,
    )


async def test_basic_chat(deepseek_model: object) -> None:
    from app.runtime.model_client import ModelCaller

    caller = ModelCaller(deepseek_model)  # type: ignore[arg-type]
    resp = await caller.create(
        system="你是测试助手。",
        messages=[{"role": "user", "content": "请只回复两个字：可用"}],
        max_tokens=32,
    )
    assert resp.blocks, "应返回文本"
    assert resp.usage.input_tokens > 0


async def test_streaming(deepseek_model: object) -> None:
    from app.runtime.model_client import ModelCaller, StreamEvent

    caller = ModelCaller(deepseek_model)  # type: ignore[arg-type]
    got_delta = False
    final = None
    async for ev in caller.stream(
        system="你是测试助手。",
        messages=[{"role": "user", "content": "从 1 数到 5，用空格分隔"}],
        max_tokens=64,
    ):
        if isinstance(ev, StreamEvent) and ev.kind == "delta":
            got_delta = True
        elif not isinstance(ev, StreamEvent):
            final = ev
    assert got_delta, "应支持流式输出"
    assert final is not None and final.usage.output_tokens > 0


async def test_tool_call(deepseek_model: object) -> None:
    from app.runtime.model_client import ModelCaller

    caller = ModelCaller(deepseek_model)  # type: ignore[arg-type]
    resp = await caller.create(
        system="你是测试助手。",
        messages=[{"role": "user", "content": "调用 echo 工具，参数 text=hello"}],
        tools=[
            {
                "name": "echo",
                "description": "回显文本",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ],
        max_tokens=128,
    )
    assert any(b.type == "tool_use" for b in resp.blocks), "应产生 tool_use"

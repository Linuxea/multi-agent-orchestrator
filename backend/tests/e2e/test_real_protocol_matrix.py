"""真实端点协议矩阵（L0）：双协议 × 关键消息形状往返。

目标：每一类 engine 真实组装的消息形状，都必须在两个协议上与真实端点完成往返。
这是「mock 测不出端点兼容性」教训的系统性防线（$defs、双层嵌套、thinking 回传
均属此类）。

运行：
    DEEPSEEK_API_KEY=sk-... uv run pytest tests/e2e/test_real_protocol_matrix.py -m real_model
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from app.runtime.model_client import ModelCaller
from app.runtime.types import ModelConfig

pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"), reason="未设置 DEEPSEEK_API_KEY"
    ),
]

TOOLS = [
    {
        "name": "echo",
        "description": "回显文本",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


def _cfg(real_platform: Any, protocol: str) -> ModelConfig:
    base = (
        os.environ.get("DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com")
        if protocol == "openai_chat"
        else os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
    )
    return ModelConfig(
        profile_id=real_platform._profile_id,
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        base_url=base,
        api_key=os.environ["DEEPSEEK_API_KEY"],
        max_output_tokens=1024,
        protocol=protocol,
    )


def _tool_use(id_: str, name: str = "echo", text: str = "hello") -> dict[str, Any]:
    return {"type": "tool_use", "id": id_, "name": name, "input": {"text": text}}


def _tool_result(id_: str, content: str = '{"ok": true}') -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": id_, "content": content}


@pytest.fixture(params=["anthropic_messages", "openai_chat"])
def caller(real_platform: Any, request: pytest.FixtureRequest) -> ModelCaller:
    return ModelCaller(_cfg(real_platform, request.param))


async def test_mixed_text_tool_turn_roundtrip(caller: ModelCaller) -> None:
    """text+tool_use 混合 assistant 轮 + tool_result 回传（双层嵌套缺陷现场）。"""
    resp = await caller.create(
        system="你是测试助手。",
        messages=[
            {"role": "user", "content": "调用 echo 工具，参数 text=hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "好的，我来调用。"},
                _tool_use("tu_m1"),
            ]},
            {"role": "user", "content": [_tool_result("tu_m1")]},
            {"role": "user", "content": "工具结果已给出，请回复：完成"},
        ],
        tools=TOOLS,
        max_tokens=256,
    )
    assert resp.blocks and resp.usage.input_tokens > 0


async def test_multi_tool_same_turn_roundtrip(caller: ModelCaller) -> None:
    """同一 assistant 轮多个 tool_use + 逐个 tool_result 回传。"""
    resp = await caller.create(
        system="你是测试助手。",
        messages=[
            {"role": "user", "content": "调用两次 echo：text=a 与 text=b"},
            {"role": "assistant", "content": [
                _tool_use("tu_a", text="a"),
                _tool_use("tu_b", text="b"),
            ]},
            {"role": "user", "content": [_tool_result("tu_a"), _tool_result("tu_b")]},
            {"role": "user", "content": "两次结果都已给出，请回复：完成"},
        ],
        tools=TOOLS,
        max_tokens=64,
    )
    assert resp.blocks


async def test_long_tool_loop_shape_roundtrip(caller: ModelCaller) -> None:
    """连续多轮工具循环历史（形状回归，防长循环累积形状劣化）。"""
    messages: list[dict[str, Any]] = [{"role": "user", "content": "循环调用 echo 五次"}]
    for i in range(1, 6):
        messages.append({"role": "assistant", "content": [_tool_use(f"tu_l{i}", text=f"n{i}")]})
        messages.append({"role": "user", "content": [_tool_result(f"tu_l{i}", json.dumps({"n": i}))]})
    messages.append({"role": "user", "content": "五次循环已完成，请回复：完成"})
    resp = await caller.create(
        system="你是测试助手。", messages=messages, tools=TOOLS, max_tokens=64
    )
    assert resp.blocks and resp.usage.input_tokens > 0


async def test_empty_text_tool_turn_roundtrip(caller: ModelCaller) -> None:
    """纯 tool_use 轮（无文本，lite 执行器塞 None 的缺陷现场）。"""
    resp = await caller.create(
        system="你是测试助手。",
        messages=[
            {"role": "user", "content": "调用 echo，text=silent"},
            {"role": "assistant", "content": [_tool_use("tu_e1", text="silent")]},
            {"role": "user", "content": [_tool_result("tu_e1")]},
            {"role": "user", "content": "请回复：完成"},
        ],
        tools=TOOLS,
        max_tokens=64,
    )
    assert resp.blocks


async def test_thinking_block_roundtrip(real_platform: Any) -> None:
    """thinking block 回传（anthropic 协议；DeepSeek 要求 tool_use 轮带回 thinking）。"""
    c = ModelCaller(_cfg(real_platform, "anthropic_messages"))
    resp = await c.create(
        system="你是测试助手。",
        messages=[
            {"role": "user", "content": "想一步，然后调用 echo，text=think"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "用户要求先思考再调用工具。", "signature": ""},
                {"type": "text", "text": "思考完毕，调用工具。"},
                _tool_use("tu_t1", text="think"),
            ]},
            {"role": "user", "content": [_tool_result("tu_t1")]},
            {"role": "user", "content": "请回复：完成"},
        ],
        tools=TOOLS,
        max_tokens=64,
    )
    assert resp.blocks

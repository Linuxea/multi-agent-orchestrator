"""模型请求消息形状契约测试。

背景（缺陷现场）：主 agent 多轮 thread 曾把 assistant content 组装成裸 dict，
DeepSeek 端点报 422「invalid type: map, expected variant identifier」，本地 mock
不校验请求形状而漏网。本组测试把 Anthropic 协议形状焊死：
content ∈ {str, block 数组}；block 必须带 type；tool_use 带 id/name/input；
tool_result 带 tool_use_id。校验在发送前本地完成（validate_anthropic_messages）。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import app.runtime.model_client as mc
from app.runtime.model_client import ModelCaller, ModelError
from app.runtime.types import ModelConfig


def _cfg(protocol: str = "anthropic_messages") -> ModelConfig:
    return ModelConfig(
        profile_id="mp-test",
        model="mock-model",
        base_url="http://127.0.0.1:9",
        api_key="k",
        max_output_tokens=256,
        protocol=protocol,  # type: ignore[arg-type]
    )


def _engine_thread() -> list[dict[str, Any]]:
    """engine._run_model_loop 真实组装的 thread 形状样本。"""
    return [
        {"role": "user", "content": "【系统唤醒】触发原因：new_user_message\n…摘要…"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "先澄清需求。"},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "ask_user",
                    "input": {"question": "做什么？", "options": ["a", "b"]},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01",
                    "content": '{"ok": true}',
                }
            ],
        },
        {"role": "assistant", "content": "已收到回答。"},
        {"role": "user", "content": "【系统唤醒】触发原因：user_answered"},
    ]


# --- 纯函数校验 ---


def test_engine_thread_shape_passes() -> None:
    mc.validate_anthropic_messages(_engine_thread())


def test_assistant_content_mixed_turn_shape() -> None:
    """engine.assistant_content（真实组装入口）各种组合的产物形状。

    回归现场：text+tool_use 混合轮曾产出列表套列表（content[0] 是 list），
    仅在模型同轮既有文本又有工具调用时触发。
    """
    from app.orchestrator.engine import assistant_content
    from app.runtime.types import ContentBlock

    text_b = ContentBlock(type="text", text="平台可用角色确认完毕。")
    empty_b = ContentBlock(type="text", text="")  # 空 text 应被过滤
    tool_b = ContentBlock(
        type="tool_use", tool_id="tu9", tool_name="propose_plan",
        tool_input={"tasks": [], "rationale": "r"},
    )

    # 混合轮（缺陷触发场景）
    mixed = assistant_content([text_b, empty_b, tool_b], [tool_b])
    assert mixed == [
        {"type": "text", "text": "平台可用角色确认完毕。"},
        {"type": "tool_use", "id": "tu9", "name": "propose_plan",
         "input": {"tasks": [], "rationale": "r"}},
    ], "混合轮 content 必须是扁平 block 数组"
    mc.validate_anthropic_messages([{"role": "assistant", "content": mixed}])

    # 纯文本轮 / 纯工具轮
    assert assistant_content([text_b], []) == [{"type": "text", "text": text_b.text}]
    assert assistant_content([], [tool_b])[0]["type"] == "tool_use"
    # 全空：content 为空数组（不发 assistant 消息由调用方决定）
    assert assistant_content([], []) == []


def test_bare_dict_content_rejected() -> None:
    """生产 422 现场：content 是单个 map 而非数组/字符串。"""
    thread = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": {"type": "text", "text": "hello"}},
    ]
    with pytest.raises(ModelError, match=r"messages\[1\].content"):
        mc.validate_anthropic_messages(thread)


def test_non_dict_block_rejected() -> None:
    thread = [{"role": "assistant", "content": ["oops"]}]
    with pytest.raises(ModelError, match="缺少 type 字段"):
        mc.validate_anthropic_messages(thread)


def test_tool_use_missing_fields_rejected() -> None:
    thread = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]}]
    with pytest.raises(ModelError, match="tool_use 缺少"):
        mc.validate_anthropic_messages(thread)


def test_tool_result_missing_id_rejected() -> None:
    thread = [{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}]
    with pytest.raises(ModelError, match="tool_result 缺少"):
        mc.validate_anthropic_messages(thread)


def test_none_content_rejected() -> None:
    thread = [{"role": "assistant", "content": None}]
    with pytest.raises(ModelError, match=r"messages\[0\].content"):
        mc.validate_anthropic_messages(thread)


# --- 两条协议路径发送前拦截（不联网） ---


async def test_anthropic_path_blocks_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """畸形消息在触达 SDK 前被拦截（远端 422 变成本地清晰错误）。"""
    called = {"create": False}

    class _FakeMessages:
        async def create(self, **kw: Any) -> None:  # pragma: no cover - 不应到达
            called["create"] = True
            raise AssertionError("畸形消息不应触达 SDK")

    class _FakeSDK:
        messages = _FakeMessages()

        async def close(self) -> None:
            return None

    def _fake_client(self: ModelCaller) -> Any:
        return _FakeSDK()

    monkeypatch.setattr(ModelCaller, "_client", _fake_client)
    caller = ModelCaller(_cfg())
    bad = [{"role": "assistant", "content": {"type": "text", "text": "x"}}]
    with pytest.raises(ModelError, match=r"messages\[0\].content"):
        await caller.create(system="s", messages=bad)
    assert not called["create"]


async def test_openai_path_blocks_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai_chat 路径输入同为 anthropic 形状线程，畸形在发送前拦截。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("畸形消息不应发出网络请求")

    def _client(self: mc._OpenAIChatCaller) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(mc._OpenAIChatCaller, "_client", _client)
    caller = ModelCaller(_cfg(protocol="openai_chat"))
    bad = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": {"type": "tool_use"}}]
    with pytest.raises(ModelError, match=r"messages\[1\].content"):
        await caller.create(system="s", messages=bad)


async def test_openai_path_converts_engine_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合法 engine thread 经 openai 转换后消息全部 content:str + tool 消息。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"},
                               "finish_reason": "stop"}], "usage": {}},
        )

    def _client(self: mc._OpenAIChatCaller) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(mc._OpenAIChatCaller, "_client", _client)
    caller = ModelCaller(_cfg(protocol="openai_chat"))
    resp = await caller.create(system="s", messages=_engine_thread())
    assert resp.blocks and resp.blocks[0].text == "ok"
    out = captured["body"]["messages"]
    assert all(isinstance(m.get("content"), (str, type(None))) for m in out)
    assert any(m.get("role") == "tool" for m in out), "tool_result 应转为 tool 消息"

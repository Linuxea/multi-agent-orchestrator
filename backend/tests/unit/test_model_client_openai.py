"""第五期 openai_chat 协议：/chat/completions 客户端（消息/工具/用量/停止映射 + 流式 + 重试）。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import app.runtime.model_client as mc
from app.runtime.model_client import ModelCaller, ModelError, StreamEvent
from app.runtime.types import ModelConfig

pytestmark = pytest.mark.asyncio


def _cfg() -> ModelConfig:
    return ModelConfig(
        profile_id="mp", model="gpt-mock", base_url="http://mock/v1",
        api_key="sk-oai", protocol="openai_chat",
    )


def _completion(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish: str = "stop",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-mock",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7},
    }


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        mc, "get_config", lambda: SimpleNamespace(model_retry_delays=(0.01, 0.01, 0.01))
    )


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _client(self: mc._OpenAIChatCaller) -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        return httpx.AsyncClient(transport=transport, trust_env=False)

    monkeypatch.setattr(mc._OpenAIChatCaller, "_client", _client)
    return seen


async def test_create_maps_text_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(content="你好"))

    _install(monkeypatch, handler)
    caller = ModelCaller(_cfg())
    resp = await caller.create(system="系统", messages=[{"role": "user", "content": "嗨"}])
    assert [b.type for b in resp.blocks] == ["text"]
    assert resp.blocks[0].text == "你好"
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 11 and resp.usage.output_tokens == 7


async def test_create_maps_tool_calls_and_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = [
        {
            "id": "call_1", "type": "function",
            "function": {"name": "echo", "arguments": "{\"text\": \"hi\"}"},
        }
    ]
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_completion(content=None, tool_calls=calls, finish="tool_calls")
        )

    _install(monkeypatch, handler)
    resp = await ModelCaller(_cfg()).create(
        system="", messages=[{"role": "user", "content": "调工具"}]
    )
    tool = next(b for b in resp.blocks if b.type == "tool_use")
    assert tool.tool_name == "echo" and tool.tool_id == "call_1"
    assert tool.tool_input == {"text": "hi"}
    assert resp.stop_reason == "tool_use"


async def test_thread_translation_and_tools_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(content="ok"))

    _install(monkeypatch, handler)
    thread: list[dict[str, Any]] = [
        {"role": "user", "content": "开始"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "调用工具"},
                {"type": "tool_use", "tool_id": "tu1", "tool_name": "echo", "tool_input": {"a": 1}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": "{\"ok\": true}"},
                {"type": "text", "text": "继续"},
            ],
        },
    ]
    tools = [{"name": "echo", "description": "回显", "input_schema": {"type": "object"}}]
    await ModelCaller(_cfg()).create(system="S", messages=thread, tools=tools)

    assert captured["path"] == "/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-oai"
    body = captured["body"]
    assert body["messages"][0] == {"role": "system", "content": "S"}
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assistant = body["messages"][2]
    assert assistant["tool_calls"][0]["function"]["name"] == "echo"
    assert body["messages"][3]["tool_call_id"] == "tu1"
    assert body["tools"][0]["function"]["parameters"] == {"type": "object"}


async def test_retry_on_500_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_completion(content="恢复"))

    _install(monkeypatch, handler)
    resp = await ModelCaller(_cfg()).create(system="", messages=[{"role": "user", "content": "x"}])
    assert resp.blocks[0].text == "恢复"
    assert state["n"] == 2


async def test_400_raises_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad"}})

    _install(monkeypatch, handler)
    with pytest.raises(ModelError):
        await ModelCaller(_cfg()).create(system="", messages=[{"role": "user", "content": "x"}])
    assert state["n"] == 1


async def test_stream_deltas_tool_args_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"思考"},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"echo","arguments":"{\\"te"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"xt\\": 1}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens": 9, "completion_tokens": 4}}',
        "data: [DONE]",
    ]
    sse = ("\n".join(lines) + "\n\n").encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    _install(monkeypatch, handler)
    caller = ModelCaller(_cfg())
    events = [ev async for ev in caller.stream(system="", messages=[{"role": "user", "content": "x"}])]

    deltas = [e for e in events if isinstance(e, StreamEvent) and e.kind == "delta"]
    assert "".join(d.text for d in deltas) == "Hello"
    thinking = [e for e in events if isinstance(e, StreamEvent) and e.kind == "thinking_delta"]
    assert thinking and thinking[0].text == "思考"

    final = events[-1]
    kinds = [b.type for b in final.blocks]
    assert kinds == ["thinking", "text", "tool_use"]
    tool = next(b for b in final.blocks if b.type == "tool_use")
    assert tool.tool_name == "echo" and tool.tool_input == {"text": 1}
    assert final.stop_reason == "tool_use"
    assert final.usage.input_tokens == 9


async def test_anthropic_protocol_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """协议默认 anthropic_messages：不装配 openai 调用器。"""
    caller = ModelCaller(ModelConfig(
        profile_id="mp", model="m", base_url="http://mock/v1", api_key="k"
    ))
    assert caller._openai is None  # noqa: SLF001

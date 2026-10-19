"""模拟模型服务（§14.1）：兼容 Anthropic Messages API 的本地 HTTP 服务。

- 支持 /v1/messages 非流式与流式（SSE）、工具调用、usage 字段。
- 按"剧本"（YAML）返回预设响应：作用域匹配 + 顺序步骤消费。
- 匹配条件：system_contains / last_message_contains / tools_contains。
- 步骤字段：text / thinking / tool_calls / stop_reason / usage / error。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


@dataclass
class Match:
    system_contains: str = ""
    last_message_contains: str = ""
    tools_contains: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Match:
        data = data or {}
        return cls(
            system_contains=str(data.get("system_contains") or ""),
            last_message_contains=str(data.get("last_message_contains") or ""),
            tools_contains=str(data.get("tools_contains") or ""),
        )

    def matches(self, system: str, last_message: str, tool_names: list[str]) -> bool:
        if self.system_contains and self.system_contains not in system:
            return False
        if self.last_message_contains and self.last_message_contains not in last_message:
            return False
        if self.tools_contains and self.tools_contains not in tool_names:
            return False
        return bool(self.system_contains or self.last_message_contains or self.tools_contains)


@dataclass
class Step:
    text: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    error_status: int = 0
    stream_delay: float = 0.0
    repeat: int = 1
    _done: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Step:
        return cls(
            text=str(data.get("text") or ""),
            thinking=str(data.get("thinking") or ""),
            tool_calls=list(data.get("tool_calls") or []),
            stop_reason=str(data.get("stop_reason") or ""),
            usage=dict(data.get("usage") or {}),
            error_status=int(data.get("error", {}).get("status_code", 0))
            if isinstance(data.get("error"), dict)
            else 0,
            stream_delay=float(data.get("stream_delay") or 0.0),
            repeat=max(1, int(data.get("repeat", 1))),
        )

    @property
    def exhausted(self) -> bool:
        return self._done >= self.repeat


@dataclass
class Scope:
    match: Match
    steps: list[Step] = field(default_factory=list)


def parse_scopes(data: dict[str, Any]) -> list[Scope]:
    scopes: list[Scope] = []
    for s in data.get("scopes") or []:
        steps = [Step.from_dict(st) for st in s.get("steps") or []]
        scopes.append(Scope(match=Match.from_dict(s.get("match")), steps=steps))
    return scopes


def load_yaml_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class MockModel:
    """剧本驱动的作用域匹配与步骤消费（线程安全）。"""

    def __init__(self) -> None:
        self.scopes: list[Scope] = []
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.request_count = 0

    def load(self, data: dict[str, Any]) -> None:
        with self.lock:
            self.scopes = parse_scopes(data)

    def load_yaml_text(self, text: str) -> None:
        self.load(yaml.safe_load(text) or {})

    def _last_message_text(self, messages: list[dict[str, Any]]) -> str:
        if not messages:
            return ""
        last = messages[-1]
        content = last.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif block.get("type") == "tool_result":
                        parts.append(str(block.get("content") or ""))
            return "\n".join(parts)
        return ""

    def next_step(self, request_body: dict[str, Any]) -> Step:
        with self.lock:
            self.request_count += 1
            system = str(request_body.get("system") or "")
            messages = request_body.get("messages") or []
            last_message = self._last_message_text(messages)
            tool_names = [str(t.get("name")) for t in request_body.get("tools") or []]
            self.requests.append(
                {"n": self.request_count, "system": system,
                 "messages": messages, "tools": tool_names}
            )

            for scope in self.scopes:
                if not scope.match.matches(system, last_message, tool_names):
                    continue
                for step in scope.steps:
                    if not step.exhausted:
                        step._done += 1
                        return self._substitute(step, messages)
            raise RuntimeError(
                "模拟模型剧本耗尽或无匹配作用域；"
                f"最近消息片段: {last_message[:120]!r}，工具: {tool_names[:5]}"
            )

    def _substitute(self, step: Step, messages: list[dict[str, Any]]) -> Step:
        """解析 tool input 中的 {{artifact:TKEY}} 占位符。

        依据：此前 read_artifact 工具结果 JSON（content 为二次序列化字符串）
        中的 artifact_id 与 task_key 字段。
        """
        import copy
        import json
        import re

        resolved: dict[str, str] = {}
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                inner = block.get("content")
                if isinstance(inner, str):
                    try:
                        inner = json.loads(inner)
                    except json.JSONDecodeError:
                        continue
                if isinstance(inner, dict) and inner.get("artifact_id") and inner.get("task_key"):
                    resolved[str(inner["task_key"])] = str(inner["artifact_id"])
        if not resolved:
            return step
        raw_placeholders = set(
            re.findall(r"\{\{artifact:([A-Za-z0-9_-]+)\}\}", json.dumps(step.tool_calls))
        )
        usable = {k: v for k, v in resolved.items() if k in raw_placeholders}
        if not usable:
            return step
        new_step = copy.deepcopy(step)
        new_step._done = step._done

        def _walk(value: Any) -> Any:
            if isinstance(value, str):
                for key, art_id in usable.items():
                    value = value.replace("{{artifact:" + key + "}}", art_id)
                return value
            if isinstance(value, list):
                return [_walk(v) for v in value]
            if isinstance(value, dict):
                return {k: _walk(v) for k, v in value.items()}
            return value

        new_step.tool_calls = [
            {"name": c.get("name"), "input": _walk(c.get("input") or {})}
            for c in new_step.tool_calls
        ]
        return new_step


def build_step_response(step: Step, model: str) -> dict[str, Any]:
    """把步骤转换为 Anthropic messages 响应 JSON。"""
    content: list[dict[str, Any]] = []
    if step.thinking:
        content.append({"type": "thinking", "thinking": step.thinking, "signature": "sig"})
    if step.text:
        content.append({"type": "text", "text": step.text})
    for _i, call in enumerate(step.tool_calls):
        content.append(
            {
                "type": "tool_use",
                "id": f"toolu_mock_{uuid.uuid4().hex[:8]}",
                "name": str(call.get("name")),
                "input": call.get("input") or {},
            }
        )
    stop_reason = step.stop_reason or ("tool_use" if step.tool_calls else "end_turn")
    usage = {
        "input_tokens": step.usage.get("input_tokens", 120),
        "output_tokens": step.usage.get("output_tokens", 80),
    }
    return {
        "id": f"msg_mock_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


def create_mock_app(state: MockModel) -> FastAPI:
    app = FastAPI(title="Mock Anthropic Model")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/messages")
    async def messages(request: Request) -> Any:
        body = await request.json()
        try:
            step = state.next_step(body)
        except RuntimeError as e:
            return JSONResponse(
                status_code=400, content={"type": "error", "error": {"message": str(e)}}
            )
        if step.error_status:
            return JSONResponse(
                status_code=step.error_status,
                content={"type": "error", "error": {"message": f"mock error {step.error_status}"}},
            )
        if step.stream_delay:
            time.sleep(step.stream_delay)  # noqa: ASYNC251

        response = build_step_response(step, str(body.get("model") or "mock-model"))
        stream = bool(body.get("stream"))
        if not stream:
            return response
        return StreamingResponse(
            _sse_stream(response, step), media_type="text/event-stream"
        )

    # --- 第五期：OpenAI 兼容端点（openai_chat 协议链路验证）---
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        body = await request.json()
        try:
            step = state.next_step(_openai_to_internal(body))
        except RuntimeError as e:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": str(e), "type": "invalid_request_error"}},
            )
        if step.error_status:
            return JSONResponse(
                status_code=step.error_status,
                content={"error": {"message": f"mock error {step.error_status}"}},
            )
        if step.stream_delay:
            time.sleep(step.stream_delay)  # noqa: ASYNC251

        response = build_step_openai_response(step, str(body.get("model") or "mock-model"))
        if not body.get("stream"):
            return response
        return StreamingResponse(
            _openai_sse_stream(response, step), media_type="text/event-stream"
        )

    return app


def _openai_to_internal(body: dict[str, Any]) -> dict[str, Any]:
    """openai chat 请求 → 内部（anthropic 风格）请求，供剧本匹配与步骤消费。"""
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for m in body.get("messages") or []:
        role = str(m.get("role") or "user")
        if role == "system":
            system_parts.append(str(m.get("content") or ""))
        elif role == "tool":
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": str(m.get("tool_call_id") or ""),
                    "content": str(m.get("content") or ""),
                }],
            })
        elif role == "assistant" and m.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for tc in m["tool_calls"]:
                fn = dict(tc.get("function") or {})
                try:
                    inp = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    inp = {}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": inp,
                })
            messages.append({"role": "assistant", "content": blocks})
        else:
            messages.append({"role": role, "content": m.get("content") or ""})
    tools = [
        {"name": str((t.get("function") or {}).get("name") or "")}
        for t in body.get("tools") or []
    ]
    return {"system": "\n".join(system_parts), "messages": messages, "tools": tools}


_OPENAI_FINISH_MAP = {"tool_use": "tool_calls", "max_tokens": "length", "end_turn": "stop"}


def build_step_openai_response(step: Step, model: str) -> dict[str, Any]:
    """把步骤转换为 OpenAI chat.completion 响应 JSON。"""
    message: dict[str, Any] = {"role": "assistant", "content": step.text or None}
    if step.tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_mock_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": str(c.get("name")),
                    "arguments": json.dumps(c.get("input") or {}, ensure_ascii=False),
                },
            }
            for c in step.tool_calls
        ]
    raw_stop = step.stop_reason or ("tool_use" if step.tool_calls else "end_turn")
    finish = _OPENAI_FINISH_MAP.get(raw_stop, "stop")
    return {
        "id": f"chatcmpl_mock_{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": step.usage.get("input_tokens", 120),
            "completion_tokens": step.usage.get("output_tokens", 80),
        },
    }


async def _openai_sse_stream(response: dict[str, Any], step: Step):
    """chat.completion.chunk SSE 流：文本分片 → 工具调用分片 → finish → usage → [DONE]。"""
    choice = response["choices"][0]

    def _data(payload: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

    yield _data({"id": response["id"], "object": "chat.completion.chunk",
                 "created": response["created"], "model": response["model"],
                 "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    text = str(choice["message"].get("content") or "")
    chunk_size = max(1, len(text) // 3) or 1
    for pos in range(0, len(text), chunk_size):
        await asyncio.sleep(0.01)
        yield _data({"choices": [{"index": 0,
                                  "delta": {"content": text[pos : pos + chunk_size]},
                                  "finish_reason": None}]})
    for i, tc in enumerate(choice["message"].get("tool_calls") or []):
        arguments = tc["function"]["arguments"]
        mid = max(1, len(arguments) // 2)
        for part in (arguments[:mid], arguments[mid:]):
            if part:
                yield _data({"choices": [{"index": 0, "finish_reason": None, "delta": {
                    "tool_calls": [{
                        "index": i, "id": tc["id"], "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": part},
                    }]
                }}]})
    yield _data({"choices": [{"index": 0, "delta": {},
                              "finish_reason": choice["finish_reason"]}]})
    yield _data({"choices": [], "usage": response["usage"]})
    yield b"data: [DONE]\n\n"


async def _sse_stream(response: dict[str, Any], step: Step):
    import asyncio

    def _chunk_lines(event: str, data: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    usage = response["usage"]
    yield _chunk_lines(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": response["id"],
                "type": "message",
                "role": "assistant",
                "model": response["model"],
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": usage["input_tokens"], "output_tokens": 0},
            },
        },
    )
    for index, block in enumerate(response["content"]):
        if block["type"] == "text":
            yield _chunk_lines(
                "content_block_start",
                {"type": "content_block_start", "index": index,
                 "content_block": {"type": "text", "text": ""}},
            )
            text = block["text"]
            chunk_size = max(1, len(text) // 3) or 1
            for pos in range(0, len(text), chunk_size):
                await asyncio.sleep(0.01)
                yield _chunk_lines(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index,
                     "delta": {"type": "text_delta", "text": text[pos : pos + chunk_size]}},
                )
            yield _chunk_lines("content_block_stop", {"type": "content_block_stop", "index": index})
        elif block["type"] == "thinking":
            yield _chunk_lines(
                "content_block_start",
                {"type": "content_block_start", "index": index,
                 "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            )
            yield _chunk_lines(
                "content_block_delta",
                {"type": "content_block_delta", "index": index,
                 "delta": {"type": "thinking_delta", "thinking": block["thinking"]}},
            )
            yield _chunk_lines("content_block_stop", {"type": "content_block_stop", "index": index})
        elif block["type"] == "tool_use":
            yield _chunk_lines(
                "content_block_start",
                {"type": "content_block_start", "index": index,
                 "content_block": {"type": "tool_use", "id": block["id"],
                                   "name": block["name"], "input": {}}},
            )
            payload = json.dumps(block["input"], ensure_ascii=False)
            mid = max(1, len(payload) // 2)
            for part in (payload[:mid], payload[mid:]):
                if part:
                    yield _chunk_lines(
                        "content_block_delta",
                        {"type": "content_block_delta", "index": index,
                         "delta": {"type": "input_json_delta", "partial_json": part}},
                    )
            yield _chunk_lines("content_block_stop", {"type": "content_block_stop", "index": index})
    yield _chunk_lines(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": response["stop_reason"]},
            "usage": {"output_tokens": usage["output_tokens"]},
        },
    )
    yield _chunk_lines("message_stop", {"type": "message_stop"})

"""模型调用封装：anthropic SDK + base_url 指向任意兼容端点（§9.5）。

- 重试：429/5xx 指数退避（1s、4s、16s），由本模块实现（SDK max_retries=0）。
- 流式：stream() 产出规范化事件；非流式 create() 返回完整响应。
- 第五期增强：ModelProtocol.openai_chat 走 /chat/completions 兼容实现（httpx），
  消息/工具/用法/停止原因在两端规范化映射。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic as anthropic_sdk
import httpx

from app.config import get_config
from app.runtime.types import ContentBlock, ModelConfig, ModelResponse
from app.schemas.common import Usage

_RETRYABLE_STATUS = {429, 500, 502, 503, 529}

_OPENAI_STOP_MAP = {"tool_calls": "tool_use", "length": "max_tokens"}


def _map_openai_stop(finish: str) -> str:
    return _OPENAI_STOP_MAP.get(finish, "end_turn")


class ModelError(RuntimeError):
    """模型调用最终失败（作为 external_error 处理，§5.7）。"""


class StreamEvent:
    """规范化流式事件。"""

    def __init__(self, kind: str, text: str = "") -> None:
        self.kind = kind  # delta | thinking_delta
        self.text = text


class ModelCaller:
    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg
        retry_delays = get_config().model_retry_delays
        self._retry_delays = retry_delays
        # 第五期：openai_chat 协议分派（anthropic_messages 走既有路径）
        self._openai = _OpenAIChatCaller(cfg) if cfg.protocol == "openai_chat" else None

    def _client(self) -> anthropic_sdk.AsyncAnthropic:
        # 注：安装版本的 anthropic SDK 使用 httpx2；timeout 直接传秒数（float）保持版本无关
        return anthropic_sdk.AsyncAnthropic(
            base_url=self.cfg.base_url,
            api_key=self.cfg.api_key or "missing-key",
            max_retries=0,
            timeout=600.0,
        )

    async def _with_retry(self, fn: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(len(self._retry_delays) + 1):
            try:
                return await fn()
            except anthropic_sdk.APIStatusError as e:
                if e.status_code in _RETRYABLE_STATUS and attempt < len(self._retry_delays):
                    last_exc = e
                    await asyncio.sleep(self._retry_delays[attempt])
                    continue
                raise ModelError(f"模型请求失败 HTTP {e.status_code}: {e.message}") from e
            except (anthropic_sdk.APIConnectionError, anthropic_sdk.APITimeoutError) as e:
                if attempt < len(self._retry_delays):
                    last_exc = e
                    await asyncio.sleep(self._retry_delays[attempt])
                    continue
                raise ModelError(f"模型连接失败: {e}") from e
        raise ModelError(f"模型请求重试耗尽: {last_exc}")

    def _params(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.cfg.model,
            "system": system,
            "messages": messages,  # type: ignore[typeddict-item]
            "max_tokens": max_tokens or self.cfg.max_output_tokens,
        }
        if self.cfg.temperature is not None:
            params["temperature"] = self.cfg.temperature
        if tools:
            params["tools"] = tools
        return params

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        if self._openai is not None:
            return await self._openai.create(
                system=system, messages=messages, tools=tools, max_tokens=max_tokens
            )
        client = self._client()
        try:
            response = await self._with_retry(
                lambda: client.messages.create(
                    **self._params(
                        system=system, messages=messages, tools=tools, max_tokens=max_tokens
                    )
                )
            )
        finally:
            await client.close()

        blocks: list[ContentBlock] = []
        for block in response.content:
            if block.type == "text":
                blocks.append(ContentBlock(type="text", text=block.text))
            elif block.type == "thinking":
                blocks.append(ContentBlock(type="thinking", text=getattr(block, "thinking", "")))
            elif block.type == "tool_use":
                blocks.append(
                    ContentBlock(
                        type="tool_use",
                        tool_id=block.id,
                        tool_name=block.name,
                        tool_input=dict(block.input or {}),
                    )
                )
        usage = Usage()
        if response.usage is not None:
            usage.input_tokens = getattr(response.usage, "input_tokens", 0) or 0
            usage.output_tokens = getattr(response.usage, "output_tokens", 0) or 0
            usage.cache_read_tokens = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        return ModelResponse(blocks=blocks, stop_reason=response.stop_reason or "end_turn", usage=usage)

    async def stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent | ModelResponse]:
        """流式调用：先产出 StreamEvent（delta/thinking_delta），最后产出一个 ModelResponse。"""
        if self._openai is not None:
            async for event in self._openai.stream(
                system=system, messages=messages, tools=tools, max_tokens=max_tokens
            ):
                yield event
            return
        client = self._client()
        params = self._params(
            system=system, messages=messages, tools=tools, max_tokens=max_tokens
        )

        response: Any = None
        try:
            response = await self._with_retry(lambda: client.messages.create(**params, stream=True))
            blocks: list[ContentBlock] = []
            current_tool: dict[str, Any] | None = None
            tool_json: list[str] = []
            text_buf: list[str] = []
            thinking_buf: list[str] = []
            usage = Usage()
            stop_reason = "end_turn"

            async for event in response:
                etype = event.type
                if etype == "message_start":
                    msg = event.message
                    if getattr(msg, "usage", None) is not None:
                        usage.input_tokens = getattr(msg.usage, "input_tokens", 0) or 0
                        usage.cache_read_tokens = (
                            getattr(msg.usage, "cache_read_input_tokens", 0) or 0
                        )
                elif etype == "content_block_start":
                    cb = event.content_block
                    if cb.type == "tool_use":
                        current_tool = {"id": cb.id, "name": cb.name}
                        tool_json = []
                elif etype == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        if delta.text:
                            text_buf.append(delta.text)
                            yield StreamEvent("delta", delta.text)
                    elif delta.type == "thinking_delta":
                        if delta.thinking:
                            thinking_buf.append(delta.thinking)
                            yield StreamEvent("thinking_delta", delta.thinking)
                    elif delta.type == "input_json_delta":
                        tool_json.append(delta.partial_json)
                elif etype == "content_block_stop":
                    if current_tool is not None:
                        raw = "".join(tool_json) or "{}"
                        try:
                            tool_input = json.loads(raw)
                        except json.JSONDecodeError:
                            tool_input = {"_raw": raw}
                        blocks.append(
                            ContentBlock(
                                type="tool_use",
                                tool_id=current_tool["id"],
                                tool_name=current_tool["name"],
                                tool_input=tool_input,
                            )
                        )
                        current_tool = None
                        tool_json = []
                elif etype == "message_delta":
                    stop_reason = event.delta.stop_reason or stop_reason
                    if getattr(event.usage, "output_tokens", None):
                        usage.output_tokens = event.usage.output_tokens
                elif etype == "message_stop":
                    break

            # 重建最终响应：thinking / text / tool_use 块按原顺序近似还原
            final_blocks: list[ContentBlock] = []
            if thinking_buf:
                final_blocks.append(ContentBlock(type="thinking", text="".join(thinking_buf)))
            if text_buf:
                final_blocks.append(ContentBlock(type="text", text="".join(text_buf)))
            final_blocks.extend(blocks)
            yield ModelResponse(blocks=final_blocks, stop_reason=stop_reason, usage=usage)
        finally:
            if response is not None:
                await response.close()
            await client.close()


class _OpenAIChatCaller:
    """OpenAI 兼容 /chat/completions 调用（第五期：ModelProtocol.openai_chat）。

    端点 = base_url + /chat/completions；重试策略与 anthropic 路径一致（429/5xx/连接错误）。
    """

    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg
        self._retry_delays = get_config().model_retry_delays

    # -- 可替换的客户端构造（测试注入 MockTransport）--
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=600.0, trust_env=False)

    def _endpoint(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.api_key or 'missing-key'}"}

    # -- 请求装配 --
    @staticmethod
    def to_openai_messages(
        system: str, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """anthropic 风格线程 → openai messages（thinking 块不外发）。"""
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            role = str(m.get("role") or "user")
            if role == "orchestrator":
                role = "assistant"  # 平台会话消息角色映射
            content = m.get("content")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []
            for block in content or []:
                b = block if isinstance(block, dict) else {}
                btype = b.get("type")
                if btype == "text" and b.get("text"):
                    text_parts.append(str(b["text"]))
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "id": str(b.get("tool_id") or f"call_{len(tool_calls)}"),
                            "type": "function",
                            "function": {
                                "name": str(b.get("tool_name") or ""),
                                "arguments": json.dumps(
                                    b.get("tool_input") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    )
                elif btype == "tool_result":
                    c = b.get("content")
                    if not isinstance(c, str):
                        c = json.dumps(c, ensure_ascii=False)
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(b.get("tool_use_id") or ""),
                            "content": str(c),
                        }
                    )
            msg: dict[str, Any] = {"role": role, "content": "\n".join(text_parts) or None}
            if role == "assistant" and tool_calls:
                msg["tool_calls"] = tool_calls
            # OpenAI 语义：tool 消息必须紧跟携带 tool_calls 的 assistant 消息
            out.extend(tool_results)
            if msg["content"] is not None or msg.get("tool_calls"):
                out.append(msg)
        return out

    @staticmethod
    def to_openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": str(t.get("name") or ""),
                    "description": str(t.get("description") or ""),
                    "parameters": dict(t.get("input_schema") or {"type": "object"}),
                },
            }
            for t in tools or []
        ]

    def _body(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": self.to_openai_messages(system, messages),
            "max_tokens": max_tokens or self.cfg.max_output_tokens,
        }
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        if tools:
            body["tools"] = self.to_openai_tools(tools)
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    # -- 带重试的请求 --
    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(len(self._retry_delays) + 1):
            client = self._client()
            try:
                resp = await client.post(
                    self._endpoint(), json=body, headers=self._headers()
                )
                if resp.status_code in _RETRYABLE_STATUS and attempt < len(self._retry_delays):
                    last_exc = ModelError(f"模型请求失败 HTTP {resp.status_code}")
                    await asyncio.sleep(self._retry_delays[attempt])
                    continue
                if resp.status_code >= 400:
                    raise ModelError(
                        f"模型请求失败 HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                return resp
            except httpx.RequestError as e:
                if attempt < len(self._retry_delays):
                    last_exc = e
                    await asyncio.sleep(self._retry_delays[attempt])
                    continue
                raise ModelError(f"模型连接失败: {e}") from e
            finally:
                await client.aclose()
        raise ModelError(f"模型请求重试耗尽: {last_exc}")

    # -- 响应解析 --
    @staticmethod
    def _parse_response(data: dict[str, Any]) -> ModelResponse:
        choices = data.get("choices") or [{}]
        message = dict(choices[0].get("message") or {})
        blocks: list[ContentBlock] = []
        text = message.get("content")
        if isinstance(text, str) and text:
            blocks.append(ContentBlock(type="text", text=text))
        elif isinstance(text, list):
            for part in text:
                if isinstance(part, dict) and part.get("text"):
                    blocks.append(ContentBlock(type="text", text=str(part["text"])))
        for tc in message.get("tool_calls") or []:
            fn = dict(tc.get("function") or {})
            try:
                tool_input = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                tool_input = {"_raw": fn.get("arguments") or ""}
            blocks.append(
                ContentBlock(
                    type="tool_use",
                    tool_id=str(tc.get("id") or ""),
                    tool_name=str(fn.get("name") or ""),
                    tool_input=dict(tool_input),
                )
            )
        usage = Usage()
        u = data.get("usage") or {}
        usage.input_tokens = int(u.get("prompt_tokens") or 0)
        usage.output_tokens = int(u.get("completion_tokens") or 0)
        cached = u.get("cache_read_input_tokens")
        if cached is None:
            details = u.get("prompt_tokens_details") or {}
            cached = details.get("cached_tokens") or 0
        usage.cache_read_tokens = int(cached or 0)
        finish = str(choices[0].get("finish_reason") or "stop")
        return ModelResponse(blocks=blocks, stop_reason=_map_openai_stop(finish), usage=usage)

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        resp = await self._post(
            self._body(
                system=system, messages=messages, tools=tools,
                max_tokens=max_tokens, stream=False,
            )
        )
        try:
            return self._parse_response(resp.json())
        finally:
            await resp.aclose()

    async def stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent | ModelResponse]:
        body = self._body(
            system=system, messages=messages, tools=tools,
            max_tokens=max_tokens, stream=True,
        )
        resp = await self._post(body)
        text_buf: list[str] = []
        thinking_buf: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish = "stop"
        try:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for choice in chunk.get("choices") or []:
                    delta = dict(choice.get("delta") or {})
                    if delta.get("content"):
                        text_buf.append(str(delta["content"]))
                        yield StreamEvent("delta", str(delta["content"]))
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        thinking_buf.append(str(reasoning))
                        yield StreamEvent("thinking_delta", str(reasoning))
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index") or 0)
                        slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = str(tc["id"])
                        fn = dict(tc.get("function") or {})
                        if fn.get("name"):
                            slot["name"] = str(fn["name"])
                        if fn.get("arguments"):
                            slot["args"] += str(fn["arguments"])
                    if choice.get("finish_reason"):
                        finish = str(choice["finish_reason"])
                u = chunk.get("usage")
                if u:
                    usage.input_tokens = int(u.get("prompt_tokens") or 0)
                    usage.output_tokens = int(u.get("completion_tokens") or 0)

            final_blocks: list[ContentBlock] = []
            if thinking_buf:
                final_blocks.append(ContentBlock(type="thinking", text="".join(thinking_buf)))
            if text_buf:
                final_blocks.append(ContentBlock(type="text", text="".join(text_buf)))
            for idx in sorted(tool_calls):
                slot = tool_calls[idx]
                try:
                    tool_input: dict[str, Any] = json.loads(slot["args"] or "{}")
                except json.JSONDecodeError:
                    tool_input = {"_raw": slot["args"]}
                final_blocks.append(
                    ContentBlock(
                        type="tool_use", tool_id=slot["id"],
                        tool_name=slot["name"], tool_input=tool_input,
                    )
                )
            yield ModelResponse(
                blocks=final_blocks, stop_reason=_map_openai_stop(finish), usage=usage
            )
        finally:
            await resp.aclose()

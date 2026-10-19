"""模型调用封装：anthropic SDK + base_url 指向任意兼容端点（§9.5）。

- 重试：429/5xx 指数退避（1s、4s、16s），由本模块实现（SDK max_retries=0）。
- 流式：stream() 产出规范化事件；非流式 create() 返回完整响应。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic as anthropic_sdk

from app.config import get_config
from app.runtime.types import ContentBlock, ModelConfig, ModelResponse
from app.schemas.common import Usage

_RETRYABLE_STATUS = {429, 500, 502, 503, 529}


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

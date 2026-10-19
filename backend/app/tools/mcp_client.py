"""MCP 客户端封装（§8）：stdio / streamable HTTP，统一连接、列举、调用。

真实 MCP 客户端 SDK（mcp 包）驱动；本环境 HTTP 走本地回环时需绕过代理
（NO_PROXY，见 conftest）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from app.tools.secrets_runtime import resolve_secret_refs

_CALL_TIMEOUT = 60.0
_CONNECT_TIMEOUT = 10.0


@dataclass
class McpCallResult:
    ok: bool
    text: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpToolInfo:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


def _result_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            parts.append(str(getattr(block, "text", "")))
        elif btype == "resource":
            res = getattr(block, "resource", None)
            parts.append(str(getattr(res, "text", "") or res))
        else:
            parts.append(json.dumps(block, ensure_ascii=False, default=str)[:2000])
    return "\n".join(p for p in parts if p)


class McpConnection:
    """一次 MCP 会话（懒连接；由 Toolbox / 工具测试持有并关闭）。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._cm: Any = None
        self._session_cm: Any = None
        self.session: Any = None
        self._owned_client: Any = None

    async def __aenter__(self) -> McpConnection:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        transport = str(self.config.get("transport") or "stdio")
        if transport == "stdio":
            params = StdioServerParameters(
                command=str(self.config["command"]),
                args=[str(a) for a in self.config.get("args") or []],
                env={str(k): str(v) for k, v in (self.config.get("env") or {}).items()} or None,
                cwd=str(self.config["cwd"]) if self.config.get("cwd") else None,
            )
            self._cm = stdio_client(params)
        elif transport in ("http", "sse"):
            headers = {str(k): str(v) for k, v in (self.config.get("headers") or {}).items()}
            if transport == "http":
                # 本版本 streamable_http_client 不收 headers：经自有 httpx2 客户端注入
                from mcp.client.streamable_http import streamable_http_client

                url = str(self.config["url"])
                if headers:
                    import httpx2 as httpx2_mod

                    self._owned_client = httpx2_mod.AsyncClient(headers=headers, trust_env=False)
                    self._cm = streamable_http_client(url, http_client=self._owned_client)
                else:
                    self._cm = streamable_http_client(url)
            else:
                from mcp.client.sse import sse_client

                url = str(self.config["url"])
                self._cm = (
                    sse_client(url, headers=headers) if headers else sse_client(url)
                )
        else:
            raise ValueError(f"未知 MCP 传输方式: {transport}")

        streams = await self._cm.__aenter__()
        read, write = streams[0], streams[1]
        self._session_cm = ClientSession(read, write)
        self.session = await self._session_cm.__aenter__()
        await asyncio.wait_for(self.session.initialize(), _CONNECT_TIMEOUT)

    async def close(self) -> None:
        try:
            if self._session_cm is not None:
                await self._session_cm.__aexit__(None, None, None)
        finally:
            if self._cm is not None:
                await self._cm.__aexit__(None, None, None)
            self._session_cm = None
            self._cm = None
            self.session = None
            if self._owned_client is not None:
                try:
                    await self._owned_client.aclose()
                except Exception:  # noqa: BLE001
                    pass
                self._owned_client = None

    async def list_tools(self) -> list[McpToolInfo]:
        result = await asyncio.wait_for(self.session.list_tools(), _CONNECT_TIMEOUT)
        infos: list[McpToolInfo] = []
        for t in result.tools:
            schema = getattr(t, "inputSchema", None) or {}
            infos.append(
                McpToolInfo(
                    name=t.name,
                    description=getattr(t, "description", "") or "",
                    input_schema=dict(schema) if isinstance(schema, dict) else {},
                )
            )
        return infos

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> McpCallResult:
        try:
            result = await asyncio.wait_for(
                self.session.call_tool(name, arguments or {}), _CALL_TIMEOUT
            )
        except TimeoutError:
            return McpCallResult(ok=False, text=f"工具 {name} 执行超时（{_CALL_TIMEOUT:.0f}s）")
        is_error = bool(getattr(result, "is_error", False))
        return McpCallResult(ok=not is_error, text=_result_text(result), raw={})


async def test_mcp_config(config: dict[str, Any]) -> tuple[bool, str, list[McpToolInfo]]:
    """连接测试：返回 (ok, 消息, 发现的工具)。config 需已解析密钥。"""
    try:
        async with McpConnection(config) as conn:
            tools = await conn.list_tools()
            return True, f"连接成功，发现 {len(tools)} 个工具", tools
    except Exception as e:  # noqa: BLE001
        return False, f"连接失败: {e}", []


async def call_once(config: dict[str, Any], tool: str, arguments: dict[str, Any]) -> McpCallResult:
    """一次性调用（工具测试 / 冒烟）。config 需已解析密钥。"""
    try:
        async with McpConnection(config) as conn:
            return await conn.call(tool, arguments)
    except Exception as e:  # noqa: BLE001
        return McpCallResult(ok=False, text=f"MCP 连接失败: {e}")


async def resolved_connection(
    session: Any, tool_config: dict[str, Any]
) -> McpConnection:
    """按 Tool.config 构建（含 $secret 解析）的连接对象（未连接）。"""
    resolved, _used = await resolve_secret_refs(session, tool_config)
    return McpConnection(dict(resolved) if not isinstance(resolved, dict) else resolved)

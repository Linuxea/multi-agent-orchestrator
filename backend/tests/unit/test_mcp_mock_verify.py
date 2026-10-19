"""mockmcp 对照真实 MCP 客户端 SDK 验证（§14.1 第二期验收）。

发现并修复的历史缺陷：serve_http 曾把 Request 导入留在函数内，叠加模块级
`from __future__ import annotations` 导致注解无法解析、请求参数被误判为查询
参数——只有真实客户端才能暴露该问题（stdio 不受影响）。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from tests.conftest import free_port

pytestmark = pytest.mark.asyncio


async def test_stdio_round_trip() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tests.mockmcp.server", "--transport", "stdio"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), 10)
            assert init.server_info.name == "mock-mcp"

            tools = await asyncio.wait_for(session.list_tools(), 10)
            names = {t.name for t in tools.tools}
            assert {"echo", "add", "write_file", "fail", "slow_timeout"} <= names

            r = await asyncio.wait_for(session.call_tool("echo", {"text": "你好"}), 10)
            assert not r.is_error
            assert r.content[0].text == "你好"

            r2 = await asyncio.wait_for(session.call_tool("add", {"a": 2, "b": 3}), 10)
            assert r2.content[0].text == "5.0"

            rf = await asyncio.wait_for(session.call_tool("fail", {}), 10)
            assert rf.is_error
            assert "故意报错" in rf.content[0].text


async def test_http_round_trip() -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    port = free_port()
    proc = subprocess.Popen(  # noqa: ASYNC220 启动子进程本身不阻塞
        [sys.executable, "-m", "tests.mockmcp.server", "--transport", "http", "--port", str(port)]
    )
    try:
        last: Exception | None = None
        for _ in range(60):
            try:
                async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as streams:
                    read, write = streams[0], streams[1]
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(), 10)
                        tools = await asyncio.wait_for(session.list_tools(), 10)
                        assert {t.name for t in tools.tools} >= {"echo", "add"}
                        r = await asyncio.wait_for(
                            session.call_tool("add", {"a": 20, "b": 22}), 10
                        )
                        assert r.content[0].text == "42.0"
                        return
            except BaseException as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(0.25)
        raise AssertionError(f"HTTP 传输连接失败: {last}")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_mcp_wrapper_connection() -> None:
    from app.tools.mcp_client import McpConnection

    conn = McpConnection(
        {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "tests.mockmcp.server", "--transport", "stdio"],
        }
    )
    async with conn:
        infos = await conn.list_tools()
        assert any(i.name == "echo" for i in infos)
        result = await conn.call("echo", {"text": "wrapped"})
        assert result.ok and result.text == "wrapped"
    assert conn.session is None  # 关闭后清理

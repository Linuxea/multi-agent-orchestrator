"""模拟 MCP 服务（§14.1）：stdio + HTTP，提供确定性工具。

工具：echo（回显）、add（加法）、write_file（写文件）、fail（故意报错）、slow_timeout（故意超时）。
协议：MCP JSON-RPC 2.0（initialize / tools/list / tools/call）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "回显输入文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "两数相加",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "write_file",
        "description": "把内容写入指定路径",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "fail",
        "description": "总是抛出错误（用于测试错误路径）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow_timeout",
        "description": "耗时 60 秒后返回（用于测试超时）",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_PROTOCOL = "2024-11-05"
_SERVER_INFO = {"name": "mock-mcp", "version": "0.1.0"}


def handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        return {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]}
    if name == "add":
        return {
            "content": [
                {"type": "text", "text": str(float(arguments.get("a", 0)) + float(arguments.get("b", 0)))}
            ]
        }
    if name == "write_file":
        path = str(arguments.get("path") or "out.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(arguments.get("content") or ""))
        return {"content": [{"type": "text", "text": f"written: {path}"}]}
    if name == "fail":
        raise RuntimeError("mock-mcp 故意报错")
    if name == "slow_timeout":
        time.sleep(60)
        return {"content": [{"type": "text", "text": "finally"}]}
    raise ValueError(f"未知工具: {name}")


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": _PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": _SERVER_INFO,
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            result = handle_tool_call(str(params.get("name")), params.get("arguments") or {})
        except Exception as e:  # noqa: BLE001
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True},
            }
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve_stdio() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = dispatch(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def serve_http(port: int) -> None:
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Mock MCP (HTTP)")

    @app.post("/mcp")
    async def mcp(request: Request) -> Any:
        body = await request.json()
        if isinstance(body, list):
            responses = [r for r in (dispatch(m) for m in body) if r is not None]
            return responses
        response = dispatch(body)
        if response is None:
            return JSONResponse(status_code=202, content={})
        return response

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟 MCP 服务")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8901)
    args = parser.parse_args()
    if args.transport == "stdio":
        serve_stdio()
    else:
        serve_http(args.port)


if __name__ == "__main__":
    main()

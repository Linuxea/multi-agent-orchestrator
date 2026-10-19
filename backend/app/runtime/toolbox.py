"""执行器工具箱（§9）：LiteExecutor 的 MCP / 自定义 HTTP / Python 工具执行。

- MCP 连接按尝试惰性建立、复用、结束时关闭；
- 所有返回给模型的文本先经脱敏器（ctx.masker）；
- custom_python 受全局设置 allow_custom_python_tools 控制，子进程执行。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import httpx

from app.runtime.types import RunContext, ToolDef
from app.tools.mcp_client import McpConnection

_RESULT_LIMIT = 4000


def _mask(ctx: RunContext, text: str) -> str:
    if ctx.masker is not None:
        return ctx.masker.mask_text(text)[:_RESULT_LIMIT]
    return text[:_RESULT_LIMIT]


class Toolbox:
    """一次尝试的工具执行器；由执行器在 start 内持有并在 finally 关闭。"""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self._connections: dict[str, McpConnection] = {}
        self._http = httpx.AsyncClient(timeout=60.0, trust_env=False)

    async def close(self) -> None:
        for conn in self._connections.values():
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._connections.clear()
        await self._http.aclose()

    def find_def(self, name: str) -> ToolDef | None:
        for d in self.ctx.tool_defs:
            if d.name == name:
                return d
        return None

    def model_tools(self) -> list[dict[str, Any]]:
        """暴露给模型的工具定义（Anthropic tools 形状）。"""
        return [
            {
                "name": d.name,
                "description": d.description or d.name,
                "input_schema": d.input_schema
                or {"type": "object", "properties": {}, "required": []},
            }
            for d in self.ctx.tool_defs
        ]

    async def call(self, defn: ToolDef, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if defn.kind == "mcp":
                return await self._call_mcp(defn, arguments)
            if defn.kind == "custom_http":
                return await self._call_http(defn, arguments)
            if defn.kind == "custom_python":
                return await self._call_python(defn, arguments)
            return {"error": f"不支持的工具类型: {defn.kind}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"工具执行异常: {e}"}

    # ------------------------------------------------------------------
    async def _call_mcp(self, defn: ToolDef, arguments: dict[str, Any]) -> dict[str, Any]:
        conn = self._connections.get(defn.slug)
        if conn is None or conn.session is None:
            conn = McpConnection(defn.config)
            await conn.connect()
            self._connections[defn.slug] = conn
        result = await conn.call(defn.subtool, arguments)
        text = _mask(self.ctx, result.text)
        return {"ok": result.ok, "result": text} if result.ok else {"error": text or "工具执行失败"}

    async def _call_http(self, defn: ToolDef, arguments: dict[str, Any]) -> dict[str, Any]:
        cfg = defn.config
        method = str(cfg.get("method") or "GET").upper()
        url = str(cfg.get("url") or "")
        headers = dict(cfg.get("headers") or {})
        body: Any = None
        template = cfg.get("body_template")
        if template:
            try:
                body = json.loads(str(template).format(**{k: str(v) for k, v in arguments.items()}))
            except Exception as e:  # noqa: BLE001
                return {"error": f"请求模板渲染失败: {e}"}
        elif method in ("POST", "PUT", "PATCH"):
            body = arguments
        resp = await self._http.request(method, url, headers=headers, json=body)
        text = _mask(self.ctx, resp.text)
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}: {text[:500]}", "status_code": resp.status_code}
        return {"ok": True, "status_code": resp.status_code, "result": text}

    async def _call_python(self, defn: ToolDef, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.ctx.allow_custom_python:
            return {"error": "自定义 Python 工具未启用（allow_custom_python_tools）"}
        code = str(defn.config.get("code") or "")
        if not code:
            return {"error": "custom_python 工具缺少 code"}
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", code,
                cwd=self.ctx.workspace,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps(arguments).encode()), timeout=60.0
            )
        except TimeoutError:
            return {"error": "Python 工具执行超时（60s）"}
        text = _mask(self.ctx, out.decode(errors="replace"))
        if proc.returncode != 0:
            return {"error": f"退出码 {proc.returncode}: {err.decode(errors='replace')[:500]}"}
        return {"ok": True, "result": text}

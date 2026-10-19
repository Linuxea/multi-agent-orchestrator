"""工具库服务（§8）：绑定、测试、发现、agent 工具物化、内置工具种子。

Tool.config 约定：
- mcp: {"transport": "stdio"|"http", "command"/"args"/"env"/"cwd" 或 "url"/"headers"}
- skill: {"body": "SKILL.md 内容"}（可选 "path" 备注来源）
- command: {"body": "斜杠命令 markdown 内容"}
- hook: {"event": "PreToolUse" 等, "command": "shell 命令", "matcher": "可选匹配"}
- custom_http: {"method", "url", "headers"?, "input_schema", "body_template"?}
- custom_python: {"code", "input_schema"}（默认停用，见 allow_custom_python_tools）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ToolKind, ToolPermission, ToolStatus
from app.ids import new_id
from app.models import Agent, Tool, ToolBinding
from app.runtime.types import ToolDef
from app.tools import mcp_client
from app.tools.secrets_runtime import resolve_secret_refs
from app.utils import utcnow

# 内置工具（CLI 内建能力的注册行；binding 可选，缺省用 default_permission）
BUILTIN_TOOLS: list[dict[str, Any]] = [
    {"slug": "bash", "name": "Shell 执行", "description": "在工作目录执行 shell 命令", "permission": "ask"},
    {"slug": "write", "name": "写文件", "description": "在工作目录写文件", "permission": "allow"},
    {"slug": "edit", "name": "改文件", "description": "在工作目录编辑文件", "permission": "allow"},
    {"slug": "web_fetch", "name": "抓取网页", "description": "抓取指定 URL 内容", "permission": "ask"},
    {"slug": "web_search", "name": "网页搜索", "description": "联网搜索", "permission": "ask"},
]


@dataclass
class AgentTools:
    """一次尝试可用的全部工具上下文（已解析密钥）。"""

    defs: list[ToolDef] = field(default_factory=list)  # lite 可直接执行的工具
    mcp_specs: dict[str, dict[str, Any]] = field(default_factory=dict)  # sdk: slug → 已解析配置
    skills: list[tuple[str, str]] = field(default_factory=list)  # (slug, SKILL.md body)
    commands: list[tuple[str, str]] = field(default_factory=list)  # (slug, 命令 body)
    hooks: list[dict[str, Any]] = field(default_factory=list)  # settings.json hooks 条目
    secret_values: list[str] = field(default_factory=list)  # 已用密钥明文（供脱敏器）


async def seed_builtin_tools(session: AsyncSession) -> list[Tool]:
    """首次初始化时创建内置工具注册行（幂等）。"""
    existing = await session.execute(select(Tool).where(Tool.kind == ToolKind.builtin))
    have = {t.slug for t in existing.scalars().all()}
    created: list[Tool] = []
    for spec in BUILTIN_TOOLS:
        if spec["slug"] in have:
            continue
        tool = Tool(
            id=new_id("tool"),
            kind=ToolKind.builtin,
            slug=spec["slug"],
            name=spec["name"],
            description=spec["description"],
            default_permission=ToolPermission(spec["permission"]),
            status=ToolStatus.ok,
        )
        session.add(tool)
        created.append(tool)
    if created:
        await session.flush()
    return created


class ToolService:
    # ------------------------------------------------------------------
    # 绑定
    # ------------------------------------------------------------------
    async def bindings_for_agent(
        self, session: AsyncSession, agent_id: str
    ) -> list[tuple[Tool, ToolBinding]]:
        result = await session.execute(
            select(Tool, ToolBinding)
            .join(ToolBinding, ToolBinding.tool_id == Tool.id)
            .where(ToolBinding.agent_id == agent_id)
            .order_by(Tool.slug)
        )
        return [(t, b) for t, b in result.all()]

    async def upsert_binding(
        self,
        session: AsyncSession,
        *,
        agent_id: str,
        tool_id: str,
        permission: str | None = None,
        enabled_subtools: list[str] | None = None,
        scope: dict[str, Any] | None = None,
    ) -> ToolBinding:
        if (await session.get(Agent, agent_id)) is None:
            raise ValueError("agent 不存在")
        if (await session.get(Tool, tool_id)) is None:
            raise ValueError("工具不存在")
        result = await session.execute(
            select(ToolBinding).where(
                ToolBinding.agent_id == agent_id, ToolBinding.tool_id == tool_id
            )
        )
        binding = result.scalar_one_or_none()
        if binding is None:
            binding = ToolBinding(
                id=new_id("tb"),
                agent_id=agent_id,
                tool_id=tool_id,
                enabled_subtools=enabled_subtools or [],
                scope=scope or {},
            )
            session.add(binding)
        if permission is not None:
            binding.permission = ToolPermission(permission)
        if enabled_subtools is not None:
            binding.enabled_subtools = enabled_subtools
        if scope is not None:
            binding.scope = scope
        await session.flush()
        return binding

    async def remove_binding(self, session: AsyncSession, agent_id: str, tool_id: str) -> bool:
        result = await session.execute(
            select(ToolBinding).where(
                ToolBinding.agent_id == agent_id, ToolBinding.tool_id == tool_id
            )
        )
        binding = result.scalar_one_or_none()
        if binding is None:
            return False
        await session.delete(binding)
        await session.flush()
        return True

    # ------------------------------------------------------------------
    # 测试与发现
    # ------------------------------------------------------------------
    async def test_tool(self, session: AsyncSession, tool: Tool) -> dict[str, Any]:
        """连接/配置测试；更新 status 与 last_test。返回测试摘要。"""
        detail = ""
        ok = False
        discovered: list[mcp_client.McpToolInfo] = []
        try:
            if tool.kind == ToolKind.mcp:
                resolved, _ = await resolve_secret_refs(session, tool.config)
                ok, detail, discovered = await mcp_client.test_mcp_config(dict(resolved))
                if ok:
                    tool.discovered = {
                        "tools": [
                            {
                                "name": t.name,
                                "description": t.description,
                                "input_schema": t.input_schema,
                            }
                            for t in discovered
                        ]
                    }
            elif tool.kind == ToolKind.custom_http:
                resolved, _ = await resolve_secret_refs(session, tool.config)
                ok, detail = await self._test_http(dict(resolved))
            elif tool.kind in (ToolKind.skill, ToolKind.command, ToolKind.hook):
                ok = bool(tool.config.get("body") or tool.config.get("command"))
                detail = "配置有效" if ok else "缺少 body/command 配置"
            else:
                ok = True
                detail = "内置工具无需连接测试"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"测试异常: {e}"

        tool.status = ToolStatus.ok if ok else ToolStatus.error
        tool.last_test = {"ok": ok, "detail": detail, "at": utcnow().isoformat()}
        await session.flush()
        return {"ok": ok, "detail": detail, "tool_count": len(discovered)}

    async def _test_http(self, config: dict[str, Any]) -> tuple[bool, str]:
        url = str(config.get("url") or "")
        if not url:
            return False, "custom_http 工具缺少 url"
        method = str(config.get("method") or "GET").upper()
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.request(
                method, url, headers=dict(config.get("headers") or {})
            )
        return resp.status_code < 500, f"HTTP {resp.status_code}"

    async def discover_tool(self, session: AsyncSession, tool: Tool) -> list[dict[str, Any]]:
        """MCP 工具发现：列出子工具并写入 discovered。"""
        if tool.kind != ToolKind.mcp:
            raise ValueError("只有 MCP 工具支持发现")
        resolved, _ = await resolve_secret_refs(session, tool.config)
        ok, detail, tools = await mcp_client.test_mcp_config(dict(resolved))
        if not ok:
            tool.status = ToolStatus.error
            tool.last_test = {"ok": False, "detail": detail, "at": utcnow().isoformat()}
            await session.flush()
            raise ValueError(detail)
        payload = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        tool.discovered = {"tools": payload}
        tool.status = ToolStatus.ok
        tool.last_test = {"ok": True, "detail": detail, "at": utcnow().isoformat()}
        await session.flush()
        return payload

    # ------------------------------------------------------------------
    # 物化
    # ------------------------------------------------------------------
    async def build_agent_tools(
        self, session: AsyncSession, agent: Agent
    ) -> AgentTools:
        """按绑定构建一次尝试的工具上下文（解析 $secret，收集脱敏值）。"""
        out = AgentTools()
        bindings = await self.bindings_for_agent(session, agent.id)
        for tool, binding in bindings:
            if tool.status == ToolStatus.disabled:
                continue
            try:
                resolved, used = await resolve_secret_refs(session, tool.config)
            except KeyError:
                # 密钥缺失：跳过该工具并继续（不阻断整个尝试）
                continue
            _ = used
            if tool.kind == ToolKind.mcp:
                out.mcp_specs[tool.slug] = dict(resolved)
                discovered = (tool.discovered or {}).get("tools") or []
                enabled = binding.enabled_subtools or []
                for sub in discovered:
                    if enabled and sub.get("name") not in enabled:
                        continue
                    out.defs.append(
                        ToolDef(
                            name=f"mcp__{tool.slug}__{sub['name']}",
                            slug=tool.slug,
                            kind="mcp",
                            description=str(sub.get("description") or ""),
                            input_schema=dict(sub.get("input_schema") or {}),
                            config=dict(resolved),
                            subtool=str(sub.get("name") or ""),
                        )
                    )
            elif tool.kind == ToolKind.custom_http:
                out.defs.append(
                    ToolDef(
                        name=tool.slug,
                        slug=tool.slug,
                        kind="custom_http",
                        description=tool.description,
                        input_schema=dict(resolved.get("input_schema") or {}),
                        config=dict(resolved),
                    )
                )
            elif tool.kind == ToolKind.custom_python:
                out.defs.append(
                    ToolDef(
                        name=tool.slug,
                        slug=tool.slug,
                        kind="custom_python",
                        description=tool.description,
                        input_schema=dict(resolved.get("input_schema") or {}),
                        config=dict(resolved),
                    )
                )
            elif tool.kind == ToolKind.skill:
                body = str(resolved.get("body") or "")
                if body:
                    out.skills.append((tool.slug, body))
            elif tool.kind == ToolKind.command:
                body = str(resolved.get("body") or "")
                if body:
                    out.commands.append((tool.slug, body))
            elif tool.kind == ToolKind.hook:
                if resolved.get("command"):
                    out.hooks.append(dict(resolved))
        # 收集密钥明文供脱敏（本尝试全部密钥，保证事件流不漏）
        from app.tools.secrets_runtime import load_masker

        masker = await load_masker(session)
        out.secret_values = masker.values
        return out

    def materialize_claude_assets(self, workspace: Path, tools: AgentTools) -> None:
        """把 Skills / Commands / Hooks 写进工作目录 .claude/（§9.2、§9.3）。

        MCP 服务器不经此文件下发（密钥仅经进程内存 → CLI 参数/env），
        避免把密钥明文落盘。
        """
        claude_dir = workspace / ".claude"
        for slug, body in tools.skills:
            skill_dir = claude_dir / "skills" / slug
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
        for slug, body in tools.commands:
            cmd_dir = claude_dir / "commands"
            cmd_dir.mkdir(parents=True, exist_ok=True)
            (cmd_dir / f"{slug}.md").write_text(body, encoding="utf-8")
        if tools.hooks:
            settings_path = claude_dir / "settings.json"
            settings: dict[str, Any] = {}
            if settings_path.exists():
                try:
                    settings = json.loads(settings_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    settings = {}
            hooks = settings.setdefault("hooks", {})
            for hook in tools.hooks:
                event = str(hook.get("event") or "")
                if not event:
                    continue
                entry: dict[str, Any] = {"hooks": [{"type": "command", "command": hook["command"]}]}
                if hook.get("matcher"):
                    entry["matcher"] = str(hook["matcher"])
                hooks.setdefault(event, []).append(entry)
            claude_dir.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
            )

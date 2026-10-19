"""工具权限判定链（§8.9）。

判定顺序（从严到宽，命中即返回）：
1. 平台内置工具（submit_report）与 CLI 只读工具 → allow
2. 解析 Tool 注册行（mcp__<slug>__<subtool> / CLI 别名 / 直接 slug）→ 不存在 deny
3. 工具停用 → deny
4. 非 builtin 工具必须绑定（ToolBinding）；builtin 无绑定时用默认权限
5. enabled_subtools 子工具范围
6. 本次运行已批准记忆（remember=this_run）
7. 绑定权限覆盖工具默认权限；高危命令表对 bash 类调用强制 deny
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ToolKind, ToolPermission, ToolStatus
from app.models import Agent, Tool, ToolBinding

# CLI 只读工具：不产生副作用，直接放行
READONLY_CLI_TOOLS: frozenset[str] = frozenset(
    {"Read", "LS", "Glob", "Grep", "TodoWrite", "NotebookRead"}
)

# CLI 工具名 → 平台 Tool.slug 映射（内置工具注册行的 slug）
CLI_TOOL_SLUGS: dict[str, str] = {
    "Bash": "bash",
    "Write": "write",
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Task": "task",
}

PLATFORM_TOOL_NAMES: frozenset[str] = frozenset(
    {"submit_report", "mcp__platform__submit_report"}
)

# 高危命令检查覆盖的 Tool.slug
_SHELL_TOOL_SLUGS: frozenset[str] = frozenset({"bash"})

_BUILTIN_KINDS: frozenset[ToolKind] = frozenset({ToolKind.builtin})


@dataclass
class PermissionDecision:
    verdict: ToolPermission  # allow / ask / deny
    basis: str  # 给用户与模型看的判定依据
    tool_slug: str = ""
    subtool: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict == ToolPermission.allow


def parse_tool_name(name: str) -> tuple[str, str]:
    """拆解工具名 → (tool_slug, subtool)。

    - mcp__<server>__<tool> → (server, tool)
    - CLI 别名（Bash 等）→ (slug, "")
    - 其他 → (name, "")
    """
    if name.startswith("mcp__"):
        rest = name[len("mcp__"):]
        if "__" in rest:
            server, sub = rest.split("__", 1)
            return server, sub
        return rest, ""
    if name in CLI_TOOL_SLUGS:
        return CLI_TOOL_SLUGS[name], ""
    return name, ""


def _dangerous_pattern_hit(settings: dict[str, Any], tool_slug: str, arguments: dict[str, Any]) -> str:
    """高危命令表命中则返回命中的模式，否则空串。"""
    if tool_slug not in _SHELL_TOOL_SLUGS:
        return ""
    patterns = [str(p) for p in (settings.get("dangerous_command_list") or []) if p]
    texts: list[str] = []
    for v in arguments.values():
        if isinstance(v, str):
            texts.append(v)
    for text in texts:
        for pat in patterns:
            if pat and pat in text:
                return pat
    return ""


async def check_tool_call(
    session: AsyncSession,
    settings: dict[str, Any],
    *,
    agent_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    remembered: bool = False,
) -> PermissionDecision:
    """按 §8.9 判定一次工具调用。remembered 由调用方维护（运行级记忆）。"""
    arguments = arguments or {}

    if tool_name in PLATFORM_TOOL_NAMES:
        return PermissionDecision(ToolPermission.allow, "平台内置工具（提交回报）")
    if tool_name in READONLY_CLI_TOOLS:
        return PermissionDecision(ToolPermission.allow, "平台只读内置工具")

    slug, subtool = parse_tool_name(tool_name)

    result = await session.execute(select(Tool).where(Tool.slug == slug))
    tool = result.scalar_one_or_none()
    if tool is None:
        return PermissionDecision(
            ToolPermission.deny, f"未注册的工具: {tool_name}", tool_slug=slug, subtool=subtool
        )
    if tool.status == ToolStatus.disabled:
        return PermissionDecision(
            ToolPermission.deny, f"工具 {slug} 已停用", tool_slug=slug, subtool=subtool
        )

    binding: ToolBinding | None = None
    bresult = await session.execute(
        select(ToolBinding).where(
            ToolBinding.agent_id == agent_id, ToolBinding.tool_id == tool.id
        )
    )
    binding = bresult.scalar_one_or_none()

    is_builtin = tool.kind in _BUILTIN_KINDS
    if binding is None and not is_builtin:
        return PermissionDecision(
            ToolPermission.deny, f"该 agent 未绑定工具 {slug}", tool_slug=slug, subtool=subtool
        )

    if binding is not None and binding.enabled_subtools and subtool:
        if subtool not in binding.enabled_subtools:
            return PermissionDecision(
                ToolPermission.deny,
                f"子工具 {subtool} 未启用（工具 {slug}）",
                tool_slug=slug,
                subtool=subtool,
            )

    if remembered:
        return PermissionDecision(
            ToolPermission.allow, "本次运行用户已批准", tool_slug=slug, subtool=subtool
        )

    permission = (binding.permission if binding and binding.permission else tool.default_permission)

    if permission == ToolPermission.deny:
        return PermissionDecision(
            ToolPermission.deny, f"工具 {slug} 权限为拒绝", tool_slug=slug, subtool=subtool
        )

    hit = _dangerous_pattern_hit(settings, slug, arguments)
    if hit:
        return PermissionDecision(
            ToolPermission.deny, f"命中高危命令规则: {hit}", tool_slug=slug, subtool=subtool
        )

    basis = (
        f"绑定权限: {permission.value}（工具 {slug}{'/' + subtool if subtool else ''}）"
        if binding is not None
        else f"内置工具默认权限: {permission.value}"
    )
    return PermissionDecision(permission, basis, tool_slug=slug, subtool=subtool)


async def agent_exists(session: AsyncSession, agent_id: str) -> bool:
    return (await session.get(Agent, agent_id)) is not None

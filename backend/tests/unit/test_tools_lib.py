"""工具库单元测试（§8）：权限链、密钥引用与脱敏、工具服务。"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from sqlalchemy import select

from app import secrets as secrets_util
from app.enums import ToolPermission, ToolStatus
from app.ids import new_id
from app.models import Agent, Secret, Tool, ToolBinding
from app.orchestrator.helpers import db_session
from app.tools.permission import check_tool_call, parse_tool_name
from app.tools.secrets_runtime import SecretMasker, resolve_secret_refs
from app.tools.service import ToolService, seed_builtin_tools
from tests.conftest import Harness

pytestmark = pytest.mark.asyncio

DEFAULTS: dict[str, Any] = {
    "dangerous_command_list": ["sudo", "rm -rf /"],
}


def _settings(**over: Any) -> dict[str, Any]:
    return {**DEFAULTS, **over}


async def _mk_tool(
    session: Any, slug: str, kind: str = "mcp", permission: str = "allow",
    config: dict[str, Any] | None = None, discovered: dict[str, Any] | None = None,
    status: str = "ok",
) -> Tool:
    tool = Tool(
        id=new_id("tool"), kind=kind, slug=slug, name=slug, description=f"{slug} 工具",
        config=config or {}, default_permission=ToolPermission(permission),
        status=ToolStatus(status), discovered=discovered,
    )
    session.add(tool)
    await session.flush()
    return tool


async def _bind(session: Any, agent: Agent, tool: Tool, permission: str | None = None,
                subtools: list[str] | None = None) -> ToolBinding:
    return await ToolService().upsert_binding(
        session, agent_id=agent.id, tool_id=tool.id,
        permission=permission, enabled_subtools=subtools,
    )


# ----------------------------------------------------------------------
# 工具名解析
# ----------------------------------------------------------------------
async def test_parse_tool_name() -> None:
    assert parse_tool_name("mcp__demo__echo") == ("demo", "echo")
    assert parse_tool_name("mcp__platform__submit_report") == ("platform", "submit_report")
    assert parse_tool_name("Bash") == ("bash", "")
    assert parse_tool_name("my_http") == ("my_http", "")


# ----------------------------------------------------------------------
# 权限链
# ----------------------------------------------------------------------
async def test_chain_platform_and_readonly(platform: Harness) -> None:
    async with db_session() as session:
        d = await check_tool_call(session, _settings(), agent_id="a", tool_name="submit_report")
        assert d.allowed
        d2 = await check_tool_call(session, _settings(), agent_id="a", tool_name="mcp__platform__submit_report")
        assert d2.allowed
        d3 = await check_tool_call(session, _settings(), agent_id="a", tool_name="Read")
        assert d3.allowed and "只读" in d3.basis


async def test_chain_unknown_tool_denied(platform: Harness) -> None:
    async with db_session() as session:
        d = await check_tool_call(session, _settings(), agent_id="a", tool_name="mcp__nope__x")
        assert not d.allowed and "未注册" in d.basis


async def test_chain_requires_binding_for_non_builtin(platform: Harness) -> None:
    h = platform
    async with db_session() as session:
        agent = await h.agent("w1", "执行员")
        tool = await _mk_tool(session, "remote-mcp")
        d = await check_tool_call(session, _settings(), agent_id=agent.id, tool_name="mcp__remote-mcp__echo")
        assert not d.allowed and "未绑定" in d.basis

        await _bind(session, agent, tool, permission="allow")
        d2 = await check_tool_call(session, _settings(), agent_id=agent.id, tool_name="mcp__remote-mcp__echo")
        assert d2.allowed

        await _bind(session, agent, tool, permission="deny")
        d3 = await check_tool_call(session, _settings(), agent_id=agent.id, tool_name="mcp__remote-mcp__echo")
        assert not d3.allowed

        await _bind(session, agent, tool, permission="ask")
        d4 = await check_tool_call(session, _settings(), agent_id=agent.id, tool_name="mcp__remote-mcp__echo")
        assert d4.verdict == ToolPermission.ask


async def test_chain_subtool_scope(platform: Harness) -> None:
    h = platform
    async with db_session() as session:
        agent = await h.agent("w2", "执行员")
        tool = await _mk_tool(session, "sc-mcp")
        await _bind(session, agent, tool, permission="allow", subtools=["echo"])
        d_ok = await check_tool_call(session, _settings(), agent_id=agent.id,
                                     tool_name="mcp__sc-mcp__echo")
        assert d_ok.allowed
        d_no = await check_tool_call(session, _settings(), agent_id=agent.id,
                                     tool_name="mcp__sc-mcp__write_file")
        assert not d_no.allowed and "未启用" in d_no.basis


async def test_chain_disabled_tool_denied(platform: Harness) -> None:
    h = platform
    async with db_session() as session:
        agent = await h.agent("w3", "执行员")
        tool = await _mk_tool(session, "off-mcp", status="disabled")
        await _bind(session, agent, tool, permission="allow")
        d = await check_tool_call(session, _settings(), agent_id=agent.id,
                                  tool_name="mcp__off-mcp__echo")
        assert not d.allowed and "停用" in d.basis


async def test_chain_builtin_defaults_and_dangerous(platform: Harness) -> None:
    async with db_session() as session:
        await seed_builtin_tools(session)
        # write 默认 allow（无需绑定）
        d = await check_tool_call(session, _settings(), agent_id="anyone", tool_name="Write")
        assert d.allowed
        # bash 默认 ask；高危命令 → deny
        d_ask = await check_tool_call(session, _settings(), agent_id="anyone", tool_name="Bash",
                                      arguments={"command": "ls"})
        assert d_ask.verdict == ToolPermission.ask
        d_danger = await check_tool_call(session, _settings(), agent_id="anyone", tool_name="Bash",
                                         arguments={"command": "sudo rm -rf /tmp/x"})
        assert not d_danger.allowed and "高危" in d_danger.basis


async def test_chain_remembered_overrides_ask(platform: Harness) -> None:
    async with db_session() as session:
        await seed_builtin_tools(session)
        d = await check_tool_call(session, _settings(), agent_id="a", tool_name="Bash",
                                  arguments={"command": "ls"}, remembered=True)
        assert d.allowed and "已批准" in d.basis


# ----------------------------------------------------------------------
# 密钥引用与脱敏
# ----------------------------------------------------------------------
async def test_secret_refs_resolve_and_mask(platform: Harness) -> None:
    async with db_session() as session:
        s = Secret(id=new_id("sec"), name="API_TOKEN", encrypted_value=secrets_util.encrypt("abcd1234xyz"))
        session.add(s)
        await session.flush()

        config = {
            "transport": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": {"$secret": "API_TOKEN"}},
        }
        resolved, used = await resolve_secret_refs(session, config)
        assert used == ["API_TOKEN"]
        assert resolved["headers"]["Authorization"] == "abcd1234xyz"
        assert s.last_used_at is not None  # 更新使用时间

        # 原配置不变（仍为引用）
        assert isinstance(config["headers"]["Authorization"], dict)

        # 缺失密钥
        with pytest.raises(KeyError):
            await resolve_secret_refs(session, {"k": {"$secret": "NOPE"}})

        # 脱敏
        masker = SecretMasker(["abcd1234xyz"])
        assert masker.mask_text("token=abcd1234xyz") == "token=***"
        assert masker.mask_data({"a": ["x abcd1234xyz"]}) == {"a": ["x ***"]}
        assert SecretMasker(["abc"]).mask_text("abc") == "abc"  # 短值（≤4）不脱敏


# ----------------------------------------------------------------------
# 工具服务：绑定 CRUD、物化
# ----------------------------------------------------------------------
async def test_bindings_and_build_agent_tools(platform: Harness) -> None:
    h = platform
    async with db_session() as session:
        agent = await h.agent("w4", "执行员")
        mcp_tool = await _mk_tool(
            session, "demo", kind="mcp",
            config={"transport": "stdio", "command": sys.executable,
                    "args": ["-m", "tests.mockmcp.server", "--transport", "stdio"]},
            discovered={"tools": [
                {"name": "echo", "description": "回显", "input_schema": {"type": "object"}},
                {"name": "add", "description": "加法", "input_schema": {"type": "object"}},
            ]},
        )
        skill = await _mk_tool(session, "skill-x", kind="skill",
                               permission="allow", config={"body": "# 技能\n说明"})
        cmd = await _mk_tool(session, "cmd-y", kind="command",
                             permission="allow", config={"body": "运行 $ARGS"})
        hook = await _mk_tool(session, "hook-z", kind="hook",
                              permission="allow",
                              config={"event": "PreToolUse", "command": "echo hi", "matcher": "Bash"})
        await _bind(session, agent, mcp_tool, permission="allow", subtools=["echo"])
        await _bind(session, agent, skill)
        await _bind(session, agent, cmd)
        await _bind(session, agent, hook)

        pairs = await ToolService().bindings_for_agent(session, agent.id)
        assert {t.slug for t, _ in pairs} == {"demo", "skill-x", "cmd-y", "hook-z"}

        # 子工具过滤：只暴露 echo
        tools = await ToolService().build_agent_tools(session, agent)
        assert [d.name for d in tools.defs] == ["mcp__demo__echo"]
        assert tools.skills == [("skill-x", "# 技能\n说明")]
        assert tools.commands == [("cmd-y", "运行 $ARGS")]
        assert tools.hooks and tools.hooks[0]["event"] == "PreToolUse"
        assert "demo" in tools.mcp_specs

        # 移除绑定
        assert await ToolService().remove_binding(session, agent.id, skill.id)
        remaining = await ToolService().bindings_for_agent(session, agent.id)
        assert {t.slug for t, _ in remaining} == {"demo", "cmd-y", "hook-z"}


async def test_materialize_claude_assets(platform: Harness, tmp_path: Any) -> None:
    tools = ToolService.__new__(ToolService)  # 无状态方法
    from app.tools.service import AgentTools

    at = AgentTools(
        skills=[("my-skill", "# SKILL")],
        commands=[("do-it", "/do-it $ARGS")],
        hooks=[{"event": "PreToolUse", "command": "guard.sh", "matcher": "Bash"}],
    )
    tools.materialize_claude_assets(tmp_path, at)
    assert (tmp_path / ".claude/skills/my-skill/SKILL.md").read_text(encoding="utf-8") == "# SKILL"
    assert (tmp_path / ".claude/commands/do-it.md").exists()
    import json

    settings = json.loads((tmp_path / ".claude/settings.json").read_text(encoding="utf-8"))
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


async def test_seed_builtin_tools_idempotent(platform: Harness) -> None:
    async with db_session() as session:
        first = await seed_builtin_tools(session)
        assert {t.slug for t in first} == {"bash", "write", "edit", "web_fetch", "web_search"}
        second = await seed_builtin_tools(session)
        assert second == []
        result = await session.execute(select(Tool).where(Tool.slug == "bash"))
        bash = result.scalar_one()
        assert bash.default_permission == ToolPermission.ask

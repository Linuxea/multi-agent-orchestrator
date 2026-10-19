"""第二期工具流 E2E（§8、§11）：权限链、MCP 调用、审批、记忆、脱敏。"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from sqlalchemy import select

import tests.scriptutil as S
from app.enums import ApprovalStatus, TaskStatus, ToolPermission, ToolStatus
from app.ids import new_id
from app.models import Approval, Tool
from app.orchestrator.helpers import db_session
from app.tools.service import ToolService
from tests.conftest import Harness

pytestmark = pytest.mark.asyncio


async def _mcp_tool(h: Harness) -> Tool:
    """注册 demo MCP 工具（stdio mockmcp）并完成发现。"""
    async with db_session() as session:
        tool = Tool(
            id=new_id("tool"),
            kind="mcp",
            slug="demo",
            name="演示 MCP",
            description="测试用 MCP 工具",
            config={
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "tests.mockmcp.server", "--transport", "stdio"],
            },
            default_permission=ToolPermission.allow,
            status=ToolStatus.ok,
        )
        session.add(tool)
        await session.flush()
        discovered = await ToolService().discover_tool(session, tool)
        assert {t["name"] for t in discovered} >= {"echo", "add", "fail"}
        return tool


async def _bind(h: Harness, agent_id: str, tool_id: str, permission: str | None = None,
                subtools: list[str] | None = None) -> None:
    async with db_session() as session:
        await ToolService().upsert_binding(
            session, agent_id=agent_id, tool_id=tool_id,
            permission=permission, enabled_subtools=subtools,
        )


async def _pending_approval(run_id: str) -> Approval:
    async with db_session() as session:
        result = await session.execute(
            select(Approval)
            .where(Approval.run_id == run_id, Approval.status == ApprovalStatus.pending)
            .order_by(Approval.created_at.desc())
        )
        approval = result.scalars().first()
        assert approval is not None, "应有待审批记录"
        return approval


def _scopes(worker_marker: str, worker_steps: list[dict[str, Any]]) -> dict[str, Any]:
    """主 agent：计划 → 派发 → 复核 → 汇总；worker 步骤按场景注入。"""
    return S.load_scopes_dict(
        S.o_scope(
            S.step_tools(S.propose_plan(S.task_spec("T1", "worker"))),
            S.step_text(),
            S.step_tools(S.dispatch("T1", "worker")),
            S.step_text(),
            S.step_tools(S.read_artifact("T1"), S.review_pass("T1")),
            S.step_text(),
            S.step_tools(S.finalize()),
        ),
        S.w_scope(worker_marker, *worker_steps),
    )


# ----------------------------------------------------------------------
# allow：绑定 allow 的 MCP 工具直接执行
# ----------------------------------------------------------------------
async def test_mcp_tool_allow_path(platform: Harness) -> None:
    h = platform
    worker = await h.agent("worker", "执行员", marker="标记W1", budget={"max_turns": 8, "max_cost": 0.1, "max_wall_seconds": 60})
    tool = await _mcp_tool(h)
    await _bind(h, worker.id, tool.id, permission="allow")

    h.scopes(_scopes(
        "标记W1",
        [
            S.step_tools(("mcp__demo__echo", {"text": "平台测试"})),
            S.step_tools(("mcp__demo__add", {"a": 1, "b": 2})),
            S.step_tools(S.report_done(summary="工具调用完成")),
        ],
    ))
    run_id, _ = await h.start()
    assert await h.wait(run_id), await h.debug_events(run_id)

    events = await h.events(run_id)
    started = [e for e in events if e.type == "tool.call.started" and e.payload["tool"] == "mcp__demo__echo"]
    finished = [e for e in events if e.type == "tool.call.finished" and e.payload["tool"] == "mcp__demo__add"]
    assert started, "echo 应产生 tool.call.started"
    assert finished and finished[0].payload["ok"]
    assert finished[0].payload["result_summary"].strip() == "3.0"

    tasks = await h.tasks(run_id)
    assert tasks["T1"].status == TaskStatus.accepted
    # attempt.started 事件携带真实工具清单
    a_started = [e for e in events if e.type == "attempt.started"]
    assert "mcp__demo__echo" in a_started[0].payload["tools"]


# ----------------------------------------------------------------------
# ask：审批通过后执行
# ----------------------------------------------------------------------
async def test_mcp_tool_approval_approve(platform: Harness) -> None:
    h = platform
    worker = await h.agent("worker", "执行员", marker="标记W2", budget={"max_turns": 8, "max_cost": 0.1, "max_wall_seconds": 60})
    tool = await _mcp_tool(h)
    await _bind(h, worker.id, tool.id, permission="ask")

    h.scopes(_scopes(
        "标记W2",
        [
            S.step_tools(("mcp__demo__echo", {"text": "需审批"})),
            S.step_tools(S.report_done(summary="审批后完成")),
        ],
    ))
    run_id, _ = await h.start()
    # 等待审批挂起（quiesce 应超时返回 False）
    assert not await h.wait(run_id, timeout=3)

    approval = await _pending_approval(run_id)
    assert approval.tool_slug == "demo"
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status == TaskStatus.awaiting_approval

    # 用户批准（remember=once）
    from app.api.routes.approvals import DecideBody, decide

    await decide(approval.id, DecideBody(approved=True, note="同意", remember="once"))

    assert await h.wait(run_id), await h.debug_events(run_id)
    events = await h.events(run_id)
    seq = [e.type for e in events]
    assert "approval.requested" in seq and "approval.decided" in seq
    assert seq.index("approval.requested") < seq.index("tool.call.started")
    started = [e for e in events if e.type == "tool.call.started" and e.payload["tool"].startswith("mcp__demo")]
    assert started, "批准后应实际执行工具"
    tasks = await h.tasks(run_id)
    assert tasks["T1"].status == TaskStatus.accepted


# ----------------------------------------------------------------------
# ask：审批拒绝 → 工具被阻断 → blocked 回报
# ----------------------------------------------------------------------
async def test_mcp_tool_approval_reject(platform: Harness) -> None:
    h = platform
    worker = await h.agent("worker", "执行员", marker="标记W3", budget={"max_turns": 8, "max_cost": 0.1, "max_wall_seconds": 60})
    tool = await _mcp_tool(h)
    await _bind(h, worker.id, tool.id, permission="ask")

    h.scopes(_scopes(
        "标记W3",
        [
            S.step_tools(("mcp__demo__echo", {"text": "被拒绝"})),
            S.step_tools(S.report_blocked("missing_tool", detail="工具权限被用户拒绝")),
        ],
    ))
    run_id, _ = await h.start()
    assert not await h.wait(run_id, timeout=3)
    approval = await _pending_approval(run_id)

    from app.api.routes.approvals import DecideBody, decide

    await decide(approval.id, DecideBody(approved=False, note="不许用", remember="once"))
    assert await h.wait(run_id), await h.debug_events(run_id)

    events = await h.events(run_id)
    blocked = [e for e in events if e.type == "tool.call.blocked"]
    assert blocked and "审批拒绝" in blocked[0].payload["reason"]
    # 工具从未真正开始执行
    started = [e for e in events if e.type == "tool.call.started" and e.payload["tool"].startswith("mcp__demo")]
    assert not started


# ----------------------------------------------------------------------
# remember=this_run：同运行同工具第二次调用免审
# ----------------------------------------------------------------------
async def test_approval_remember_this_run(platform: Harness) -> None:
    h = platform
    worker = await h.agent("worker", "执行员", marker="标记W4", budget={"max_turns": 8, "max_cost": 0.1, "max_wall_seconds": 60})
    tool = await _mcp_tool(h)
    await _bind(h, worker.id, tool.id, permission="ask")

    h.scopes(_scopes(
        "标记W4",
        [
            S.step_tools(("mcp__demo__echo", {"text": "第一次"})),
            S.step_tools(("mcp__demo__echo", {"text": "第二次"})),
            S.step_tools(S.report_done(summary="两次调用完成")),
        ],
    ))
    run_id, _ = await h.start()
    assert not await h.wait(run_id, timeout=3)
    approval = await _pending_approval(run_id)

    from app.api.routes.approvals import DecideBody, decide

    await decide(approval.id, DecideBody(approved=True, remember="this_run"))
    assert await h.wait(run_id), await h.debug_events(run_id)

    events = await h.events(run_id)
    approvals = [e for e in events if e.type == "approval.requested"]
    started = [e for e in events if e.type == "tool.call.started" and e.payload["tool"] == "mcp__demo__echo"]
    assert len(approvals) == 1, "第二次调用应命中运行级记忆，不再审批"
    assert len(started) == 2


# ----------------------------------------------------------------------
# remember=always_for_agent：绑定权限持久化为 allow
# ----------------------------------------------------------------------
async def test_approval_remember_always_for_agent(platform: Harness) -> None:
    h = platform
    worker = await h.agent("worker", "执行员", marker="标记W5", budget={"max_turns": 8, "max_cost": 0.1, "max_wall_seconds": 60})
    tool = await _mcp_tool(h)
    await _bind(h, worker.id, tool.id, permission="ask")

    h.scopes(_scopes(
        "标记W5",
        [
            S.step_tools(("mcp__demo__echo", {"text": "首次"})),
            S.step_tools(("mcp__demo__echo", {"text": "再次"})),
            S.step_tools(S.report_done(summary="完成")),
        ],
    ))
    run_id, _ = await h.start()
    assert not await h.wait(run_id, timeout=3)
    approval = await _pending_approval(run_id)

    from app.api.routes.approvals import DecideBody, decide

    await decide(approval.id, DecideBody(approved=True, remember="always_for_agent"))
    assert await h.wait(run_id), await h.debug_events(run_id)

    # 绑定权限已持久化为 allow
    async with db_session() as session:
        pairs = await ToolService().bindings_for_agent(session, worker.id)
        binding = [b for t, b in pairs if t.slug == "demo"][0]
        assert binding.permission == ToolPermission.allow


# ----------------------------------------------------------------------
# deny：高危命令
# ----------------------------------------------------------------------
async def test_dangerous_command_denied(platform: Harness) -> None:
    h = platform
    worker = await h.agent("worker", "执行员", marker="标记W6", budget={"max_turns": 8, "max_cost": 0.1, "max_wall_seconds": 60})
    async with db_session() as session:
        from app.tools.service import seed_builtin_tools

        await seed_builtin_tools(session)
        result = await session.execute(select(Tool).where(Tool.slug == "bash"))
        bash = result.scalar_one()
    await _bind(h, worker.id, bash.id, permission="allow")

    h.scopes(_scopes(
        "标记W6",
        [
            S.step_tools(("Bash", {"command": "sudo rm -rf /tmp/anything"})),
            S.step_tools(S.report_blocked("missing_tool", detail="bash 被高危规则拦截")),
        ],
    ))
    run_id, _ = await h.start()
    assert await h.wait(run_id), await h.debug_events(run_id)

    events = await h.events(run_id)
    blocked = [e for e in events if e.type == "tool.call.blocked"]
    assert blocked and "高危" in blocked[0].payload["reason"]
    started = [e for e in events if e.type == "tool.call.started" and e.payload["tool"] == "Bash"]
    assert not started


# ----------------------------------------------------------------------
# custom_http 工具
# ----------------------------------------------------------------------
async def test_custom_http_tool(platform: Harness) -> None:
    h = platform
    worker = await h.agent("worker", "执行员", marker="标记W7", budget={"max_turns": 8, "max_cost": 0.1, "max_wall_seconds": 60})
    async with db_session() as session:
        tool = Tool(
            id=new_id("tool"),
            kind="custom_http",
            slug="health_check",
            name="健康检查",
            description="GET 健康端点",
            config={
                "method": "GET",
                "url": f"{h.model_url}/health",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            default_permission=ToolPermission.allow,
            status=ToolStatus.ok,
        )
        session.add(tool)
        await session.flush()
        tool_id = tool.id
    await _bind(h, worker.id, tool_id, permission="allow")

    h.scopes(_scopes(
        "标记W7",
        [
            S.step_tools(("health_check", {})),
            S.step_tools(S.report_done(summary="健康检查完成")),
        ],
    ))
    run_id, _ = await h.start()
    assert await h.wait(run_id), await h.debug_events(run_id)

    events = await h.events(run_id)
    finished = [e for e in events if e.type == "tool.call.finished" and e.payload["tool"] == "health_check"]
    assert finished and finished[0].payload["ok"]


# ----------------------------------------------------------------------
# 审批超时：expired + 按拒绝处理
# ----------------------------------------------------------------------
async def test_approval_timeout_expires(platform: Harness) -> None:
    h = platform
    await h.patch_settings({"approval_timeout_minutes": 0.002})  # ≈0.12s
    worker = await h.agent("worker", "执行员", marker="标记W8", budget={"max_turns": 8, "max_cost": 0.1, "max_wall_seconds": 60})
    tool = await _mcp_tool(h)
    await _bind(h, worker.id, tool.id, permission="ask")

    h.scopes(_scopes(
        "标记W8",
        [
            S.step_tools(("mcp__demo__echo", {"text": "超时"})),
            S.step_tools(S.report_blocked("missing_tool", detail="审批超时被拒")),
        ],
    ))
    run_id, _ = await h.start()
    assert await h.wait(run_id), await h.debug_events(run_id)

    events = await h.events(run_id)
    seq = [e.type for e in events]
    assert "approval.requested" in seq and "approval.expired" in seq
    blocked = [e for e in events if e.type == "tool.call.blocked"]
    assert blocked and "超时" in blocked[0].payload["reason"]
    async with db_session() as session:
        result = await session.execute(
            select(Approval).where(Approval.run_id == run_id)
        )
        assert result.scalars().first().status == ApprovalStatus.expired


# ----------------------------------------------------------------------
# 密钥脱敏：工具参数含密钥值 → 事件流掩码
# ----------------------------------------------------------------------
async def test_secret_masking_in_events(platform: Harness) -> None:
    h = platform
    from app import secrets as secrets_util
    from app.models import Secret

    worker = await h.agent("worker", "执行员", marker="标记W9", budget={"max_turns": 8, "max_cost": 0.1, "max_wall_seconds": 60})
    async with db_session() as session:
        session.add(Secret(
            id=new_id("sec"), name="API_TOKEN",
            encrypted_value=secrets_util.encrypt("sk-live-maskme-9999"),
        ))
        await session.flush()
    tool = await _mcp_tool(h)
    await _bind(h, worker.id, tool.id, permission="allow")

    h.scopes(_scopes(
        "标记W9",
        [
            S.step_tools(("mcp__demo__echo", {"text": "token=sk-live-maskme-9999"})),
            S.step_tools(S.report_done(summary="完成")),
        ],
    ))
    run_id, _ = await h.start()
    assert await h.wait(run_id), await h.debug_events(run_id)

    events = await h.events(run_id)
    started = [e for e in events if e.type == "tool.call.started" and e.payload["tool"] == "mcp__demo__echo"]
    assert started
    assert "sk-live-maskme-9999" not in str(started[0].payload)
    assert "***" in str(started[0].payload["arguments"])

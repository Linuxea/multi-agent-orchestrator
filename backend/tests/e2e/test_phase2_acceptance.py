"""第二期验收清单（§14.3）：SdkExecutor、工具库、审批链、密钥脱敏、模型体检。

清单条目（对照规格逐条落测试）：
1. SdkExecutor 注册进容器，无 Claude Code 运行时时明确崩溃提示；
2. MCP 工具：注册 → 测试/发现（stdio 与 HTTP 两种传输、失败记录）；
3. 权限链与审批流：见 test_phase2_tool_flow（allow/ask/deny/记忆/超时）；
4. 审批门 resolve 直接唤醒等待方（API decide 之外的等价入口）；
5. 密钥：$secret 解析与事件脱敏（见 test_phase2_tool_flow / test_tools_lib）；
6. 模型健康检查：四项检查 + 时延 + 汇总结论（§9.6）；
7. 工作目录物化：inputs/ out/ work/ + TASK.md（§9.2、§9.3）。
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from sqlalchemy import select

from app.enums import ApprovalStatus, RememberScope
from app.ids import new_id
from app.models import Approval, Tool
from app.orchestrator.helpers import db_session
from app.runtime.types import EvCrashed, ModelConfig, RunContext
from app.schemas.common import Budget
from app.tools.service import ToolService
from tests.conftest import Harness, free_port

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# 1. SdkExecutor 注册与运行时缺失降级
# ----------------------------------------------------------------------
async def test_sdk_executor_registered(platform: Harness) -> None:
    assert "sdk" in platform.container.executors.available()


async def test_sdk_executor_crashes_clearly_without_runtime(
    platform: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.runtime.sdk as sdk_mod

    monkeypatch.setattr(sdk_mod, "sdk_runtime_available", lambda: False)
    from app.runtime.sdk import SdkExecutor

    ctx = RunContext(
        run_id="r", task_key="T1", attempt_id="a1", attempt_no=1, agent_slug="w",
        workspace="/tmp", system_prompt="sys", first_input="go",
        budget=Budget(max_turns=5, max_cost=5.0, max_wall_seconds=30),
        model=ModelConfig(profile_id="mp", model="m", base_url="http://x", api_key="k"),
    )
    events = [ev async for ev in SdkExecutor().start(ctx)]
    assert events and isinstance(events[0], EvCrashed)
    assert "Claude Code CLI 不可用" in events[0].error


# ----------------------------------------------------------------------
# 2. MCP 工具：stdio / HTTP 测试 + 发现 + 失败记录
# ----------------------------------------------------------------------
async def test_mcp_tool_test_and_discover_stdio(platform: Harness) -> None:
    async with db_session() as session:
        tool = Tool(
            id=new_id("tool"), kind="mcp", slug="m1", name="m1",
            config={
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "tests.mockmcp.server", "--transport", "stdio"],
            },
        )
        session.add(tool)
        await session.flush()
        result = await ToolService().test_tool(session, tool)
        assert result["ok"] and result["tool_count"] >= 5
        assert tool.status.value == "ok"
        names = {t["name"] for t in (tool.discovered or {}).get("tools", [])}
        assert "echo" in names


async def test_mcp_tool_test_http(platform: Harness) -> None:
    import subprocess

    import httpx

    port = free_port()
    proc = subprocess.Popen(  # noqa: ASYNC220 启动子进程本身不阻塞
        [sys.executable, "-m", "tests.mockmcp.server", "--transport", "http", "--port", str(port)]
    )
    try:
        for _ in range(60):  # 等服务就绪
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    await client.post(
                        f"http://127.0.0.1:{port}/mcp",
                        json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
                        timeout=1.0,
                    )
                    break
            except Exception:
                await asyncio.sleep(0.1)
        async with db_session() as session:
            tool = Tool(
                id=new_id("tool"), kind="mcp", slug="m2", name="m2",
                config={"transport": "http", "url": f"http://127.0.0.1:{port}/mcp"},
            )
            session.add(tool)
            await session.flush()
            result = await ToolService().test_tool(session, tool)
            assert result["ok"], result["detail"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_mcp_tool_test_failure_recorded(platform: Harness) -> None:
    async with db_session() as session:
        tool = Tool(
            id=new_id("tool"), kind="mcp", slug="m3", name="m3",
            config={"transport": "stdio", "command": "/nonexistent/binary", "args": []},
        )
        session.add(tool)
        await session.flush()
        result = await ToolService().test_tool(session, tool)
        assert not result["ok"]
        assert tool.status.value == "error"
        assert tool.last_test and tool.last_test["ok"] is False


# ----------------------------------------------------------------------
# 4. 审批门 resolve 直接唤醒（API decide 之外的等价入口）
# ----------------------------------------------------------------------
async def test_approval_gate_resolve_wakes_waiter(platform: Harness) -> None:
    h = platform
    await h.agent("w", "执行员")  # 保证库非空（与审批单无直接关系）
    gate = h.container.scheduler.approvals

    class _Ctx:
        run_id = "run_x"
        attempt_id = "att_x"
        agent_slug = "w"
        task_key = "T1"

    async def _request() -> tuple[bool, str]:
        return await gate.request(
            _Ctx(),  # type: ignore[arg-type]
            tool_slug="demo",
            tool_name="mcp__demo__echo",
            action="mcp__demo__echo",
            arguments_masked={"text": "x"},
            reason="绑定权限: ask",
            risk_note="测试",
        )

    request_task = asyncio.create_task(_request())
    approval_id = ""
    for _ in range(60):  # 等审批单落库
        await asyncio.sleep(0.05)
        async with db_session() as session:
            result = await session.execute(
                select(Approval).where(Approval.status == ApprovalStatus.pending)
            )
            approval = result.scalars().first()
            if approval is not None:
                approval_id = str(approval.id)
                break
    assert approval_id, "审批单未创建"

    async with db_session() as session:
        approval = await session.get(Approval, approval_id)
        assert approval is not None
        approval.status = ApprovalStatus.rejected
    await gate.resolve(approval_id, approved=False, note="直接拒绝", remember=RememberScope.once)

    approved, note = await asyncio.wait_for(request_task, 5)
    assert approved is False
    assert "直接拒绝" in note


# ----------------------------------------------------------------------
# 6. 模型健康检查（§9.6 扩展）
# ----------------------------------------------------------------------
async def test_model_profile_check(platform: Harness) -> None:
    from app.api.routes.model_profiles import check_profile

    # 体检的三个模型调用走"测试助手"作用域剧本（§9.6 顺序：对话 → 流式 → 工具）
    platform.scopes({
        "scopes": [
            {
                "match": {"system_contains": "测试助手"},
                "steps": [
                    {"text": "OK"},
                    {"text": "1 2 3 4 5"},
                    {"tool_calls": [{"name": "echo", "input": {"text": "hello"}}]},
                ],
            }
        ]
    })
    result = await check_profile(platform._profile_id)
    assert result["ok"] is True
    names = [c["name"] for c in result["checks"]]
    assert names == ["基础对话", "流式输出", "工具调用", "用量返回"]
    for c in result["checks"]:
        assert c["passed"], c
        if c["name"] != "用量返回":
            assert isinstance(c["latency_ms"], int)


# ----------------------------------------------------------------------
# 7. 工作目录物化
# ----------------------------------------------------------------------
async def test_workspace_materialization(platform: Harness) -> None:
    h = platform
    await h.agent(
        "worker", "执行员",
        budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30},
    )
    import tests.scriptutil as S

    h.scopes(S.load_scopes_dict(
        S.o_scope(
            S.step_tools(S.propose_plan(S.task_spec("T1", "worker"))),
            S.step_text(),
            S.step_tools(S.dispatch("T1", "worker")),
            S.step_text(),
            S.step_tools(S.read_artifact("T1"), S.review_pass("T1")),
            S.step_text(),
            S.step_tools(S.finalize()),
        ),
        S.w_scope("worker", S.step_tools(S.report_done())),
    ))
    run_id, _ = await h.start()
    assert await h.wait(run_id), await h.debug_events(run_id)

    events = await h.events(run_id, ["attempt.started"])
    workspace = events[0].payload["workspace_path"]
    from pathlib import Path

    root = Path(workspace)
    assert (root / "TASK.md").exists()
    assert all((root / d).is_dir() for d in ("inputs", "out", "work"))

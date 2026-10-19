"""SdkExecutor 单元测试：选项装配、事件翻译、submit_report、权限门、兜底回报。"""

from __future__ import annotations

from typing import Any

import pytest

import app.runtime.sdk as sdk_mod
import tests.fakesdk as F
from app.runtime.sdk import SdkExecutor
from app.runtime.types import (
    EvCrashed,
    EvOutputDelta,
    EvOutputTurn,
    EvReported,
    EvThinking,
    EvToolFinished,
    EvToolStarted,
    EvUsage,
    GateResult,
    ModelConfig,
    RunContext,
)
from app.schemas.common import Budget

pytestmark = pytest.mark.asyncio


class _Masker:
    def mask_text(self, text: str) -> str:
        return text.replace("TOPSECRET", "***")

    def mask_data(self, data: Any) -> Any:
        return data


def _ctx(**over: Any) -> RunContext:
    gate_calls: list[tuple[str, dict[str, Any]]] = []

    async def gate(ctx: RunContext, name: str, args: dict[str, Any]) -> GateResult:
        gate_calls.append((name, args))
        if name in getattr(gate, "deny", set()):
            return GateResult(approved=False, basis="测试拒绝")
        return GateResult(approved=True, basis="测试放行")

    base: dict[str, Any] = {
        "run_id": "run_test",
        "task_key": "T1",
        "attempt_id": "att_test",
        "attempt_no": 1,
        "agent_slug": "worker",
        "agent_id": "agent_w",
        "workspace": "/tmp",
        "system_prompt": "系统提示词",
        "first_input": "请开始执行任务",
        "budget": Budget(max_turns=10, max_cost=5.0, max_wall_seconds=60),
        "model": ModelConfig(
            profile_id="mp", model="m1", base_url="http://127.0.0.1:9/v1",
            api_key="sk-test",
        ),
        "masker": _Masker(),
        "tool_gate": gate,
        "mcp_specs": {"demo": {"transport": "stdio", "command": "py", "args": ["x"]}},
    }
    base.update(over)
    ctx = RunContext(**base)
    gate.deny = set()  # type: ignore[attr-defined]
    ctx.gate_calls = gate_calls  # type: ignore[attr-defined]
    ctx._gate_fn = gate  # type: ignore[attr-defined]
    return ctx


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    F.reset()
    mod = F.build_fake_sdk()
    monkeypatch.setattr(sdk_mod, "_sdk_module", mod)
    monkeypatch.setattr(sdk_mod, "sdk_runtime_available", lambda: True)


async def _collect(executor: SdkExecutor, ctx: RunContext) -> list[Any]:
    out = []
    async for ev in executor.start(ctx):
        out.append(ev)
    return out


async def test_options_assembly_and_happy_report() -> None:
    ctx = _ctx()
    executor = SdkExecutor()
    async def inject() -> None:
        import asyncio

        while not F.LAST["clients"]:  # noqa: ASYNC110 等待执行器创建伪客户端
            await asyncio.sleep(0.01)
        c = F.LAST["clients"][0]
        c.script = [
            F.stream_text("开头"),
            F.text_msg("第一段输出"),
            {"call_platform": {"status": "done", "summary": "完成", "attempted": ["x"]}},
            F.result_msg(),
        ]

    import asyncio

    inject_task = asyncio.create_task(inject())
    events = await _collect(executor, ctx)
    await inject_task

    # 选项装配断言
    client_obj = F.LAST["clients"][0]
    opts = client_obj.options
    assert opts.cwd == ctx.workspace
    assert opts.system_prompt == {"type": "custom", "prompt": ctx.system_prompt}
    assert opts.setting_sources == ["project"]
    assert opts.permission_mode == "acceptEdits"
    assert opts.model == "m1"
    assert int(opts.max_turns) == 10
    assert "platform" in opts.mcp_servers
    assert opts.mcp_servers["demo"] == {
        "type": "stdio", "command": "py", "args": ["x"], "env": {},
    }
    assert opts.env["ANTHROPIC_BASE_URL"] == ctx.model.base_url
    assert opts.env["ANTHROPIC_API_KEY"] == "sk-test"
    assert opts.env["ANTHROPIC_MODEL"] == "m1"

    # 事件翻译断言
    types_seq = [type(e) for e in events]
    assert EvOutputDelta in types_seq
    assert EvOutputTurn in types_seq
    assert EvUsage in types_seq
    assert EvReported in types_seq
    reported = [e for e in events if isinstance(e, EvReported)][0]
    assert reported.report["status"] == "done"
    assert reported.report["_usage"]["input_tokens"] > 0
    assert client_obj.disconnected


async def test_can_use_tool_denied_by_gate() -> None:
    ctx = _ctx()
    ctx._gate_fn.deny = {"Bash"}  # type: ignore[attr-defined]
    executor = SdkExecutor()

    async def inject() -> None:
        import asyncio

        while not F.LAST["clients"]:  # noqa: ASYNC110 等待执行器创建伪客户端
            await asyncio.sleep(0.01)
        c = F.LAST["clients"][0]
        c.script = [
            {"invoke": ("Bash", {"command": "ls"})},
            {"call_platform": {"status": "done", "summary": "完成", "attempted": ["x"]}},
            F.result_msg(),
        ]

    import asyncio

    task = asyncio.create_task(inject())
    await _collect(executor, ctx)
    await task
    assert F.LAST["can_use_calls"] == [("Bash", {"command": "ls"})]
    # 拒绝结果返回给 CLI（PermissionResultDeny 携带判定依据）
    denied = [r for r in F.LAST["permission_results"] if isinstance(r, F.PermissionResultDeny)]
    assert denied and "测试拒绝" in denied[0].message


async def test_tool_events_translation() -> None:
    ctx = _ctx()
    executor = SdkExecutor()

    async def inject() -> None:
        import asyncio

        while not F.LAST["clients"]:  # noqa: ASYNC110 等待执行器创建伪客户端
            await asyncio.sleep(0.01)
        c = F.LAST["clients"][0]
        c.script = [
            F.tool_use_msg("tu1", "mcp__demo__echo", {"text": "TOPSECRET"}),
            F.tool_result_msg("tu1", "TOPSECRET echoed"),
            F.thinking_msg("思考中"),
            {"call_platform": {"status": "done", "summary": "完成", "attempted": ["x"]}},
            F.result_msg(),
        ]

    import asyncio

    task = asyncio.create_task(inject())
    events = await _collect(executor, ctx)
    await task

    started = [e for e in events if isinstance(e, EvToolStarted)]
    assert started and started[0].tool == "mcp__demo__echo"
    assert started[0].arguments == {"text": "***"}  # 脱敏

    finished = [e for e in events if isinstance(e, EvToolFinished)]
    assert finished and finished[0].ok and finished[0].result_summary == "*** echoed"

    thinking = [e for e in events if isinstance(e, EvThinking)]
    assert thinking


async def test_invalid_report_not_captured_then_fallback() -> None:
    ctx = _ctx()
    executor = SdkExecutor()

    async def inject() -> None:
        import asyncio

        while not F.LAST["clients"]:  # noqa: ASYNC110 等待执行器创建伪客户端
            await asyncio.sleep(0.01)
        c = F.LAST["clients"][0]
        c.script = [
            # 非法回报：blocked 必须附 blocker（模型校验拒绝）
            {"call_platform": {"status": "blocked", "summary": "缺 blocker"}},
            F.result_msg(),
        ]

    import asyncio

    task = asyncio.create_task(inject())
    events = await _collect(executor, ctx)
    await task
    reported = [e for e in events if isinstance(e, EvReported)]
    assert reported
    # 非法回报未捕获 → 走 no_report 兜底
    assert "未调用 submit_report" in reported[0].report["summary"]


async def test_result_error_crashes() -> None:
    ctx = _ctx()
    executor = SdkExecutor()

    async def inject() -> None:
        import asyncio

        while not F.LAST["clients"]:  # noqa: ASYNC110 等待执行器创建伪客户端
            await asyncio.sleep(0.01)
        F.LAST["clients"][0].script = [F.result_msg(is_error=True, result="CLI 崩了")]

    import asyncio

    task = asyncio.create_task(inject())
    events = await _collect(executor, ctx)
    await task
    crashed = [e for e in events if isinstance(e, EvCrashed)]
    assert crashed and "CLI 崩了" in crashed[0].error


async def test_runtime_unavailable_crashes_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdk_mod, "sdk_runtime_available", lambda: False)
    ctx = _ctx()
    events = await _collect(SdkExecutor(), ctx)
    assert events and isinstance(events[0], EvCrashed)
    assert "Claude Code CLI 不可用" in events[0].error

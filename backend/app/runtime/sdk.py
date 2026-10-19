"""SdkExecutor（§9.1 sdk 类型）：基于 claude-agent-sdk 的 Claude Code 运行时。

- 会话：ClaudeSDKClient，cwd=工作目录，setting_sources 仅 project（不读宿主用户配置）；
- 模型：CLI 经 ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL 环境变量指向 agent 的模型配置；
- 工具：submit_report 作为进程内 SDK MCP 服务（mcp__platform__submit_report）；
  绑定的 MCP 服务器以解析密钥后的配置经 options.mcp_servers 下发（不落盘）；
- 权限：permission_mode=acceptEdits（工作目录内文件编辑免审），其余全部经
  can_use_tool → 平台权限链（§8.9）→ ask 时走审批门（§11）；
- 预算：max_turns 传 CLI；费用/时长由本执行器跟踪并兜底回报（§5.4）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.runtime.reporting import budget_exhausted_report, no_report_fallback
from app.runtime.types import (
    EvCrashed,
    EvOutputDelta,
    EvOutputTurn,
    EvReported,
    EvSessionBound,
    EvThinking,
    EvTimedOut,
    EvToolFinished,
    EvToolStarted,
    EvUsage,
    ExecEvent,
    GateResult,
    RunContext,
)
from app.schemas.common import Usage
from app.schemas.contracts import TaskReport
from app.schemas.inline import inline_refs

_sdk_module: Any = None

SUBMIT_REPORT_DESCRIPTION = (
    "提交结构化回报并结束任务。这是结束任务的唯一方式。"
    "status 可选 done/partial/blocked/failed；blocked/failed 必须附 blocker。"
)

_BUDGET_REMINDER_RATIO = 0.8


def _load_sdk() -> Any:
    """惰性导入 claude_agent_sdk（测试用 monkeypatch 替换为假 SDK）。"""
    global _sdk_module
    if _sdk_module is None:
        import claude_agent_sdk

        _sdk_module = claude_agent_sdk
    return _sdk_module


def reset_sdk_cache() -> None:
    global _sdk_module
    _sdk_module = None


def cli_candidate_paths() -> list[str]:
    env_path = os.environ.get("PLATFORM_CLAUDE_CLI_PATH", "")
    candidates = [env_path] if env_path else []
    candidates += [shutil.which("claude") or ""]
    local = Path.home() / ".claude" / "local" / "claude"
    candidates.append(str(local))
    return [c for c in candidates if c]


def sdk_runtime_available() -> bool:
    """SDK 可导入且能找到 Claude Code CLI。"""
    try:
        _load_sdk()
    except Exception:  # noqa: BLE001
        return False
    return any(Path(p).exists() for p in cli_candidate_paths())


def _cli_env(ctx: RunContext) -> dict[str, str]:
    cfg = ctx.model
    key = cfg.api_key or "missing-key"
    env = dict(os.environ)
    env.update(
        {
            "ANTHROPIC_BASE_URL": cfg.base_url,
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_API_KEY": key,
            "ANTHROPIC_MODEL": cfg.model,
            "ANTHROPIC_SMALL_FAST_MODEL": cfg.model,
            "CLAUDE_CODE_SUBAGENT_MODEL": cfg.model,
            "DISABLE_TELEMETRY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_COST_WARNINGS": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_BUG_COMMAND": "1",
            "NO_PROXY": env.get("NO_PROXY", "127.0.0.1,localhost"),
        }
    )
    return env


def _mcp_server_config(spec: dict[str, Any]) -> dict[str, Any]:
    transport = str(spec.get("transport") or "stdio")
    if transport == "stdio":
        return {
            "type": "stdio",
            "command": str(spec.get("command") or ""),
            "args": [str(a) for a in spec.get("args") or []],
            "env": {str(k): str(v) for k, v in (spec.get("env") or {}).items()},
        }
    if transport in ("http", "sse"):
        return {
            "type": transport,
            "url": str(spec.get("url") or ""),
            "headers": {str(k): str(v) for k, v in (spec.get("headers") or {}).items()},
        }
    raise ValueError(f"未知 MCP 传输方式: {transport}")


def _usage_from_dict(data: dict[str, Any] | None) -> Usage:
    usage = Usage()
    if not data:
        return usage
    usage.input_tokens = int(data.get("input_tokens") or 0)
    usage.output_tokens = int(data.get("output_tokens") or 0)
    usage.cache_read_tokens = int(
        data.get("cache_read_input_tokens") or data.get("cache_read_tokens") or 0
    )
    return usage


class SdkExecutor:
    name = "sdk"

    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}
        self._interrupted: set[str] = set()

    # ------------------------------------------------------------------
    async def start(self, ctx: RunContext) -> AsyncIterator[ExecEvent]:
        async for event in self._run_session(ctx, resume_session_id=None):
            yield event

    def resume(self, ctx: RunContext) -> AsyncIterator[ExecEvent] | None:
        """会话恢复（第五期）：自动重试尝试凭源尝试的 executor_session_id 续跑。

        ctx.resume_session_id 为空表示无会话可续，返回 None 由调度器回退 start()。
        """
        if ctx.resume_session_id is None:
            return None
        return self._run_session(ctx, resume_session_id=ctx.resume_session_id)

    async def _run_session(
        self, ctx: RunContext, resume_session_id: str | None
    ) -> AsyncIterator[ExecEvent]:
        try:
            sdk = _load_sdk()
        except Exception as e:  # noqa: BLE001
            yield EvCrashed(error=f"claude-agent-sdk 不可用: {e}")
            return
        if not sdk_runtime_available():
            yield EvCrashed(
                error=(
                    "Claude Code CLI 不可用：未在 PATH 或 ~/.claude/local 找到 claude。"
                    "可设置 PLATFORM_CLAUDE_CLI_PATH 指定路径。"
                )
            )
            return

        usage = Usage()
        turns = 0
        last_output = ""
        captured: dict[str, Any] | None = None
        tool_names: dict[str, str] = {}
        budget_reminded = False
        session_bound = False

        async def _emit_session(msg: Any) -> AsyncIterator[ExecEvent]:
            """ResultMessage / SystemMessage 携带 session_id 时产出 EvSessionBound。"""
            nonlocal session_bound
            sid = str(getattr(msg, "session_id", "") or "")
            if sid and not session_bound:
                session_bound = True
                yield EvSessionBound(session_id=sid)

        # submit_report 进程内工具
        async def _submit_report(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal captured
            data: dict[str, Any] = dict(args[0]) if args and isinstance(args[0], dict) else {}
            data.update(kwargs)
            try:
                report = TaskReport(**data)
            except ValidationError as e:
                return {"received": False, "error": "回报不合法", "details": str(e.errors()[:5])}
            payload = report.model_dump(mode="json")
            payload["_usage"] = usage.model_dump(mode="json")
            captured = payload
            return {"received": True, "status": report.status.value}

        submit_tool = sdk.tool(
            name="submit_report",
            description=SUBMIT_REPORT_DESCRIPTION,
            input_schema=inline_refs(TaskReport.model_json_schema()),
        )(_submit_report)

        mcp_servers: dict[str, Any] = {
            "platform": sdk.create_sdk_mcp_server(name="platform", tools=[submit_tool])
        }
        for slug, spec in ctx.mcp_specs.items():
            mcp_servers[slug] = _mcp_server_config(spec)

        async def _can_use_tool(tool_name: str, inp: dict[str, Any], _context: Any) -> Any:
            gate = ctx.tool_gate
            if gate is None:
                return sdk.PermissionResultAllow()
            result = await gate(ctx, tool_name, inp)
            if isinstance(result, GateResult) and result.approved:
                return sdk.PermissionResultAllow()
            basis = result.basis if isinstance(result, GateResult) else "平台拒绝"
            return sdk.PermissionResultDeny(message=f"平台拒绝该工具调用: {basis}")

        options = sdk.ClaudeAgentOptions(
            cwd=ctx.workspace,
            system_prompt={"type": "custom", "prompt": ctx.system_prompt},
            setting_sources=["project"],
            permission_mode="acceptEdits",
            can_use_tool=_can_use_tool,
            mcp_servers=mcp_servers,
            env=_cli_env(ctx),
            model=ctx.model.model,
            max_turns=int(ctx.budget.max_turns or 24),
            include_partial_messages=True,
        )
        if resume_session_id is not None:
            options.resume = resume_session_id
        cli_path = os.environ.get("PLATFORM_CLAUDE_CLI_PATH", "")
        if cli_path:
            options.cli_path = cli_path

        try:
            client = sdk.ClaudeSDKClient(options=options)
            await client.connect(prompt=ctx.first_input)
        except Exception as e:  # noqa: BLE001
            yield EvCrashed(error=f"Claude Code 会话建立失败: {e}", last_output=last_output)
            return

        self._clients[ctx.attempt_id] = client
        started = time.monotonic()
        hard_deadline = started + float(ctx.budget.max_wall_seconds or 3600)

        def _mask(text: str) -> str:
            if ctx.masker is not None:
                return ctx.masker.mask_text(text)
            return text

        try:
            while True:
                if ctx.attempt_id in self._interrupted:
                    return
                remaining = hard_deadline - time.monotonic()
                if remaining <= 0:
                    with contextlib.suppress(Exception):
                        await client.interrupt()
                    yield EvTimedOut(
                        error=f"超过最长执行时长 {ctx.budget.max_wall_seconds}s"
                    )
                    return
                try:
                    msg = await asyncio.wait_for(
                        _next_message(client.receive_response()), timeout=remaining
                    )
                except TimeoutError:
                    with contextlib.suppress(Exception):
                        await client.interrupt()
                    yield EvTimedOut(
                        error=f"超过最长执行时长 {ctx.budget.max_wall_seconds}s"
                    )
                    return
                except StopAsyncIteration:
                    break

                kind = type(msg).__name__

                if kind == "StreamEvent":
                    event = dict(getattr(msg, "event", None) or {})
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield EvOutputDelta(text=str(delta["text"]))
                    elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                        yield EvThinking(text=str(delta["thinking"]))
                    continue

                if kind == "AssistantMessage":
                    turns += 1
                    text_parts: list[str] = []
                    for block in list(getattr(msg, "content", None) or []):
                        btype = type(block).__name__
                        if btype == "TextBlock" and str(getattr(block, "text", "") or ""):
                            text_parts.append(str(block.text))
                        elif btype == "ThinkingBlock" and str(
                            getattr(block, "thinking", "") or ""
                        ):
                            yield EvThinking(text=_mask(str(block.thinking)))
                        elif btype == "ToolUseBlock":
                            tool_names[str(block.id)] = str(block.name)
                            yield EvToolStarted(
                                tool=str(block.name),
                                arguments={
                                    k: _mask(str(v)) if isinstance(v, str) else v
                                    for k, v in dict(block.input or {}).items()
                                },
                            )
                    if text_parts:
                        last_output = "\n".join(text_parts)
                        yield EvOutputTurn(text=_mask(last_output))
                    msg_usage = _usage_from_dict(dict(getattr(msg, "usage", None) or {}))
                    if msg_usage.input_tokens or msg_usage.output_tokens:
                        usage.input_tokens += msg_usage.input_tokens
                        usage.output_tokens += msg_usage.output_tokens
                        usage.cache_read_tokens += msg_usage.cache_read_tokens
                        usage.turns = turns
                        usage.wall_seconds = time.monotonic() - started
                        usage.cost = ctx.model.cost_of(usage)
                        yield EvUsage(usage=Usage(**usage.model_dump()))
                    # 预算 80% 提醒：注入到下一条输入（不打断当前回合）
                    ratio = usage.ratio(ctx.budget)
                    if (
                        ratio is not None
                        and ratio >= _BUDGET_REMINDER_RATIO
                        and not budget_reminded
                        and captured is None
                    ):
                        budget_reminded = True
                        with contextlib.suppress(Exception):
                            await client.query(
                                "预算提示：用量已超过 80%，请尽快收敛并调用 submit_report 提交回报。"
                            )
                    if ratio is not None and ratio >= 1.0 and captured is None:
                        with contextlib.suppress(Exception):
                            await client.interrupt()
                    continue

                if kind == "UserMessage":
                    for block in list(getattr(msg, "content", None) or []):
                        if type(block).__name__ == "ToolResultBlock":
                            tid = str(getattr(block, "tool_use_id", ""))
                            name = tool_names.get(tid, "tool")
                            is_error = bool(getattr(block, "is_error", False))
                            content = getattr(block, "content", "")
                            if isinstance(content, (list, tuple)):
                                summary = " ".join(
                                    str(getattr(c, "text", "") or c) for c in content
                                )
                            else:
                                summary = str(content or "")
                            yield EvToolFinished(
                                tool=name,
                                ok=not is_error,
                                duration_ms=0,
                                result_summary=_mask(summary)[:200],
                            )
                    continue

                if kind == "ResultMessage":
                    async for ev in _emit_session(msg):
                        yield ev
                    final_usage = _usage_from_dict(dict(getattr(msg, "usage", None) or {}))
                    if final_usage.input_tokens or final_usage.output_tokens:
                        usage.input_tokens = max(usage.input_tokens, final_usage.input_tokens)
                        usage.output_tokens = max(usage.output_tokens, final_usage.output_tokens)
                    usage.turns = int(getattr(msg, "num_turns", 0) or turns or 1)
                    usage.wall_seconds = time.monotonic() - started
                    usage.cost = ctx.model.cost_of(usage)
                    yield EvUsage(usage=Usage(**usage.model_dump()))

                    if captured is not None:
                        captured["_usage"] = usage.model_dump(mode="json")
                        yield EvReported(report=captured)
                        return
                    if bool(getattr(msg, "is_error", False)):
                        error_text = str(
                            getattr(msg, "result", None)
                            or getattr(msg, "errors", None)
                            or "Claude Code 执行出错"
                        )
                        yield EvCrashed(error=_mask(error_text), last_output=_mask(last_output))
                        return
                    final_ratio = usage.ratio(ctx.budget)
                    if final_ratio is not None and final_ratio >= 1.0:
                        yield EvReported(
                            report=budget_exhausted_report(
                                usage, usage.turns, final_ratio, last_output
                            )
                        )
                        return
                    yield EvReported(report=no_report_fallback(usage, usage.turns, last_output))
                    return

                # SystemMessage / RateLimitEvent / 其他：忽略（仅捕获 session_id）
                async for ev in _emit_session(msg):
                    yield ev
        finally:
            self._clients.pop(ctx.attempt_id, None)
            self._interrupted.discard(ctx.attempt_id)
            with contextlib.suppress(Exception):
                await client.disconnect()

    # ------------------------------------------------------------------
    async def send_input(self, ctx: RunContext, text: str) -> None:
        client = self._clients.get(ctx.attempt_id)
        if client is not None:
            await client.query(text)

    async def interrupt(self, attempt_id: str) -> None:
        self._interrupted.add(attempt_id)
        client = self._clients.get(attempt_id)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.interrupt()


async def _next_message(iterator: AsyncIterator[Any]) -> Any:
    async for msg in iterator:
        return msg
    raise StopAsyncIteration

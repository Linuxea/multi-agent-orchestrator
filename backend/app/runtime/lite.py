"""LiteExecutor（§9.8）：自写的工具调用循环，适用于纯文本类 agent。

- 工具：submit_report（平台注入）+ 绑定的 MCP / 自定义 HTTP / Python 工具（第二期）；
- 权限：每个工具调用经 tool_gate（§8.9 权限链 + §11 审批）；
- 交付物：允许回报中内联内容，由调度器写入 out/ 并登记；
- 预算：轮次/费用/时长达到 80% 提醒、100% 自动生成 budget_exhausted 回报（§5.4）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from pydantic import ValidationError

from app.runtime.model_client import ModelCaller, ModelError
from app.runtime.reporting import budget_exhausted_report
from app.runtime.toolbox import Toolbox
from app.runtime.types import (
    EvCrashed,
    EvOutputTurn,
    EvReported,
    EvThinking,
    EvTimedOut,
    EvToolFinished,
    EvToolStarted,
    EvUsage,
    ExecEvent,
    ModelResponse,
    RunContext,
)
from app.schemas.common import Usage
from app.schemas.contracts import TaskReport
from app.schemas.inline import inline_refs

SUBMIT_REPORT_TOOL: dict[str, Any] = {
    "name": "submit_report",
    "description": (
        "提交结构化回报并结束任务。这是结束任务的唯一方式。"
        "status 可选 done/partial/blocked/failed；blocked/failed 必须附 blocker。"
    ),
    "input_schema": inline_refs(TaskReport.model_json_schema()),
}

_BUDGET_REMINDER_RATIO = 0.8
_INVALID_REPORT_LIMIT = 2


def _cast_event(obj: object) -> asyncio.Event | None:
    """RunContext.pause_event 类型为 object（避免跨层导入），运行时是 asyncio.Event。"""
    return obj if isinstance(obj, asyncio.Event) else None


class LiteExecutor:
    name = "lite"

    def __init__(self) -> None:
        self._cancelled: set[str] = set()

    async def start(self, ctx: RunContext) -> AsyncIterator[ExecEvent]:
        caller = ModelCaller(ctx.model)
        toolbox = Toolbox(ctx)
        messages: list[dict[str, Any]] = [{"role": "user", "content": ctx.first_input}]
        usage = Usage()
        turns = 0
        invalid_reports = 0
        budget_reminded = False
        started = time.monotonic()
        hard_deadline = (ctx.budget.max_wall_seconds or 3600) + started
        warn_deadline = hard_deadline - 60
        grace_prompted = False
        last_output = ""

        max_turns = ctx.budget.max_turns or 24
        tools = [SUBMIT_REPORT_TOOL, *toolbox.model_tools()]

        try:
            while True:
                if ctx.attempt_id in self._cancelled:
                    return

                # 暂停门（§5.6：执行完当前工具调用后暂停）
                gate = _cast_event(ctx.pause_event)
                if gate is not None:
                    await gate.wait()

                now = time.monotonic()
                if now >= hard_deadline:
                    yield EvTimedOut(error=f"超过最长执行时长 {ctx.budget.max_wall_seconds}s")
                    return
                if now >= warn_deadline and not grace_prompted:
                    grace_prompted = True
                    messages.append(
                        {"role": "user", "content": "请立即调用 submit_report 提交回报（即将到达时间上限）。"}
                    )

                try:
                    turn_text: list[str] = []
                    response = None
                    async for event in caller.stream(
                        system=ctx.system_prompt, messages=messages, tools=tools
                    ):
                        if isinstance(event, ModelResponse):
                            response = event
                        elif event.kind == "delta":
                            from app.runtime.types import EvOutputDelta

                            turn_text.append(event.text)
                            yield EvOutputDelta(text=event.text)
                        elif event.kind == "thinking_delta":
                            yield EvThinking(text=event.text)
                except ModelError as e:
                    yield EvCrashed(error=str(e), last_output=last_output)
                    return

                assert response is not None
                turns += 1
                usage.turns = turns
                usage.input_tokens += response.usage.input_tokens
                usage.output_tokens += response.usage.output_tokens
                usage.cache_read_tokens += response.usage.cache_read_tokens
                usage.wall_seconds = time.monotonic() - started
                usage.cost = ctx.model.cost_of(usage)
                yield EvUsage(usage=Usage(**usage.model_dump()))

                text = "".join(turn_text)
                if text:
                    last_output = text
                    yield EvOutputTurn(text=text)

                # 预算 80% 提醒（§9.4）
                ratio = usage.ratio(ctx.budget)
                if ratio is not None and ratio >= _BUDGET_REMINDER_RATIO and not budget_reminded:
                    budget_reminded = True
                    messages.append(
                        {"role": "user", "content": "预算提示：用量已超过 80%，请尽快收敛并提交回报。"}
                    )
                if ratio is not None and ratio >= 1.0 or turns >= max_turns:
                    # 运行时兜底：自动生成 budget_exhausted 回报（§5.4）
                    yield EvReported(
                        report=budget_exhausted_report(usage, turns, ratio, last_output)
                    )
                    return

                tool_blocks = [b for b in response.blocks if b.type == "tool_use"]
                if not tool_blocks:
                    # 未调用 submit_report 即结束 → 兜底追问一次，再不提交则 crashed（§5.4）
                    if invalid_reports >= _INVALID_REPORT_LIMIT:
                        yield EvCrashed(error="子 agent 未提交回报", last_output=last_output)
                        return
                    invalid_reports += 1
                    messages.append({"role": "assistant", "content": text or "（空输出）"})
                    messages.append(
                        {
                            "role": "user",
                            "content": "你必须调用 submit_report 工具提交结构化回报才能结束任务。请现在提交。",
                        }
                    )
                    continue

                # 记录 assistant 轮（含工具调用）；text 为空时不产生 None 元素（端点会拒收）
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            *([{"type": "text", "text": text}] if text else []),
                            *[
                                {
                                    "type": "tool_use",
                                    "id": b.tool_id,
                                    "name": b.tool_name,
                                    "input": b.tool_input,
                                }
                                for b in tool_blocks
                            ],
                        ]
                        or [{"type": "text", "text": text or "（空输出）"}],
                    }
                )

                tool_results: list[dict[str, Any]] = []
                done = False
                for block in tool_blocks:
                    if block.tool_name != "submit_report":
                        holder: dict[str, Any] = {}
                        async for ev in self._run_platform_tool(
                            ctx, toolbox, block.tool_name, block.tool_input, holder
                        ):
                            yield ev
                        tool_results.append(_tool_result(block.tool_id, holder["result"]))
                        continue
                    yield EvToolStarted(tool="submit_report", arguments=block.tool_input)
                    t0 = time.monotonic()
                    try:
                        report = TaskReport(**block.tool_input)
                    except ValidationError as e:
                        invalid_reports += 1
                        ok = False
                        result: dict[str, Any] = {
                            "error": "回报不合法，请修正后重新提交",
                            "details": str(e.errors()[:5]),
                        }
                        if invalid_reports >= _INVALID_REPORT_LIMIT:
                            yield EvCrashed(error="回报多次不合法", last_output=last_output)
                            return
                    else:
                        ok = True
                        result = {"received": True, "status": report.status.value}
                        done = True
                    yield EvToolFinished(
                        tool="submit_report",
                        ok=ok,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                        result_summary=str(result)[:200],
                    )
                    tool_results.append(_tool_result(block.tool_id, result))
                    if done:
                        usage.tool_calls += 1
                        payload = report.model_dump(mode="json")
                        payload["_usage"] = usage.model_dump(mode="json")
                        yield EvReported(report=payload)
                        return

                messages.append({"role": "user", "content": tool_results})
        finally:
            await toolbox.close()

    async def _run_platform_tool(
        self,
        ctx: RunContext,
        toolbox: Toolbox,
        name: str,
        arguments: dict[str, Any],
        holder: dict[str, Any],
    ) -> AsyncIterator[ExecEvent]:
        """权限门 → 执行绑定工具。

        - 拒绝：结果携带 error（tool.call.blocked 由调度器门发出，此处不发事件）；
        - 放行：发 EvToolStarted → 执行 → EvToolFinished。
        """
        t0 = time.monotonic()
        gate = ctx.tool_gate
        if gate is not None:
            decision = await gate(ctx, name, arguments)
            if decision is not None and not decision.approved:
                holder["result"] = {"error": f"权限拒绝：{decision.basis}"}
                return
        defn = toolbox.find_def(name)
        if defn is None:
            holder["result"] = {"error": f"未知工具: {name}"}
            return
        yield EvToolStarted(tool=name, arguments=arguments)
        result = await toolbox.call(defn, arguments)
        yield EvToolFinished(
            tool=name,
            ok="error" not in result,
            duration_ms=int((time.monotonic() - t0) * 1000),
            result_summary=str(result.get("result") or result.get("error") or "")[:200],
        )
        holder["result"] = result

    async def send_input(self, ctx: RunContext, text: str) -> None:
        """LiteExecutor 的输入注入在第一期由调度器转发（对话在 start 协程内）。"""
        # 会话消息由 start 内部管理；这里通过取消-重启策略兜底，第一期不实现热注入。
        return None

    async def interrupt(self, attempt_id: str) -> None:
        self._cancelled.add(attempt_id)

    def resume(self, ctx: RunContext) -> AsyncIterator[ExecEvent] | None:
        return None


def _tool_result(tool_id: str, output: dict[str, Any]) -> dict[str, Any]:
    import json

    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": json.dumps(output, ensure_ascii=False),
    }

"""§5.4 运行时兜底回报：预算耗尽时自动生成结构化回报（lite/sdk 共用）。"""

from __future__ import annotations

from typing import Any

from app.schemas.common import Usage
from app.schemas.contracts import TaskReport


def budget_exhausted_report(
    usage: Usage, turns: int, ratio: float | None, last_output: str
) -> dict[str, Any]:
    report = TaskReport(
        status="partial",
        summary=f"预算耗尽自动回报（使用率 {ratio:.0%}，{turns} 轮）。已完成部分见交付物。",
        blocker={"type": "budget_exhausted", "detail": "预算耗尽", "needs": "追加预算或采纳部分结果"},
        attempted=[f"已完成 {turns} 轮模型调用"],
    )
    payload = report.model_dump(mode="json")
    payload["_usage"] = usage.model_dump(mode="json")
    payload["_last_output"] = last_output[-2000:]
    return payload


def no_report_fallback(usage: Usage, turns: int, last_output: str) -> dict[str, Any]:
    """对话结束但未提交回报的兜底（§5.4）。"""
    report = TaskReport(
        status="partial",
        summary=f"子 agent 结束对话但未调用 submit_report（{turns} 轮），按部分完成兜底。",
        blocker={"type": "budget_exhausted", "detail": "未提交回报", "needs": "复核后返工"},
        attempted=[f"完成 {turns} 轮"],
    )
    payload = report.model_dump(mode="json")
    payload["_usage"] = usage.model_dump(mode="json")
    payload["_last_output"] = last_output[-2000:]
    return payload

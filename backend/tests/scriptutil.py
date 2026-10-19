"""测试剧本构建工具。"""

from __future__ import annotations

from typing import Any

CRITERION = "至少给出 3 个有来源的结论"
CRITERION_B = "结论必须与调研数据一致"


def rationale(params: dict[str, Any]) -> dict[str, Any]:
    return {"rationale": "测试决策理由", **params}


def step_text(text: str = "收到，按此执行。") -> dict[str, Any]:
    return {"text": text}


def step_tools(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for name, params in calls:
        # submit_report 是子 agent 的平台工具（非主 agent 控制工具），不需要 rationale，
        # 且 TaskReport schema 为 extra=forbid
        if name != "submit_report":
            params = {"rationale": "测试决策理由", **params}
        out.append({"name": name, "input": params})
    return {"tool_calls": out}


def o_scope(*steps: dict[str, Any]) -> dict[str, Any]:
    """主 agent 作用域（系统提示词含"主管"）。"""
    return {"match": {"system_contains": "主管"}, "steps": list(steps)}


def w_scope(marker: str, *steps: dict[str, Any]) -> dict[str, Any]:
    """子 agent 作用域（按 agent 提示词中的标记词区分）。"""
    return {"match": {"system_contains": marker}, "steps": list(steps)}


def propose_plan(*tasks: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return ("propose_plan", {"tasks": list(tasks)})


def task_spec(
    key: str,
    agent_slug: str,
    title: str | None = None,
    depends_on: list[str] | None = None,
    criteria: list[str] | None = None,
    optional: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title or f"任务 {key}",
        "agent_slug": agent_slug,
        "depends_on": depends_on or [],
        "acceptance_criteria": criteria or [CRITERION],
        "optional": optional,
    }


def dispatch(
    task_key: str,
    agent_slug: str,
    *,
    objective: str = "完成测试任务",
    criteria: list[str] | None = None,
    rework_feedback: str | None = None,
    context: str = "测试背景",
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "task_key": task_key,
        "agent_slug": agent_slug,
        "objective": objective,
        "context": context,
        "acceptance_criteria": criteria or [CRITERION],
        "deliverable_format": "markdown",
        "deliverable_name": f"{task_key.lower()}_report.md",
        "selection_rationale": f"{agent_slug} 适合此任务",
    }
    if rework_feedback:
        params["rework_feedback"] = rework_feedback
    return ("dispatch", params)


def review_pass(
    task_key: str,
    criteria: list[str] | None = None,
    gaps_note: str = "",
    verdict: str = "pass",
) -> tuple[str, dict[str, Any]]:
    crit = criteria or [CRITERION]
    review: dict[str, Any] = {
        "verdict": verdict,
        "criteria": [{"criterion": c, "result": "pass"} for c in crit],
    }
    if gaps_note:
        review["gaps_note"] = gaps_note
    return ("review", {"task_key": task_key, "review": review})


def review_fail(
    task_key: str,
    criteria: list[str] | None = None,
    required_changes: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    crit = criteria or [CRITERION]
    return (
        "review",
        {
            "task_key": task_key,
            "review": {
                "verdict": "fail",
                "criteria": [{"criterion": c, "result": "fail"} for c in crit],
                "required_changes": required_changes or ["补充缺失内容"],
            },
        },
    )


def read_artifact(task_key: str, mode: str = "summary") -> tuple[str, dict[str, Any]]:
    return ("read_artifact", {"task_key": task_key, "mode": mode})


def finalize(
    answer: str = "最终答复：报告已完成。",
    citations: list[dict[str, Any]] | None = None,
    gaps: str = "",
) -> tuple[str, dict[str, Any]]:
    return (
        "finalize",
        {
            "answer": answer,
            "citations": citations if citations is not None else [],
            "gaps": gaps,
        },
    )


def report_done(
    summary: str = "已按验收标准完成调研",
    *,
    criteria: list[str] | None = None,
    content: str = "# 报告\n\n内容与来源。",
    self_met: bool = True,
) -> tuple[str, dict[str, Any]]:
    crit = criteria or [CRITERION]
    return (
        "submit_report",
        {
            "status": "done",
            "summary": summary,
            "deliverables": [
                {"name": "report.md", "content": content, "summary": "报告正文"}
            ],
            "criteria_self_check": [
                {"criterion": c, "met": self_met} for c in crit
            ],
            "evidence": ["https://example.com/source"],
            "attempted": ["检索了示例来源"],
        },
    )


def report_blocked(
    blocker_type: str,
    detail: str = "缺少条件",
    needs: str = "",
    summary: str = "无法继续",
) -> tuple[str, dict[str, Any]]:
    return (
        "submit_report",
        {
            "status": "blocked",
            "summary": summary,
            "blocker": {"type": blocker_type, "detail": detail, "needs": needs},
            "attempted": ["尝试了现有条件下的推进"],
        },
    )


def report_partial(
    summary: str = "完成了一部分",
    blocker_type: str = "missing_info",
    detail: str = "缺少部分信息",
) -> tuple[str, dict[str, Any]]:
    return (
        "submit_report",
        {
            "status": "partial",
            "summary": summary,
            "deliverables": [
                {"name": "partial.md", "content": "# 部分结果", "summary": "部分结果"}
            ],
            "criteria_self_check": [{"criterion": CRITERION, "met": False}],
            "blocker": {"type": blocker_type, "detail": detail, "needs": "补充信息"},
        },
    )


def report_failed(detail: str = "确认无法完成") -> tuple[str, dict[str, Any]]:
    return (
        "submit_report",
        {
            "status": "failed",
            "summary": "失败",
            "blocker": {"type": "out_of_scope", "detail": detail},
            "attempted": ["尝试后确认做不到"],
        },
    )


def ask_user(
    question: str = "需要用户确认：接受哪种方案？",
    blocking: list[str] | None = None,
    options: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    return (
        "ask_user",
        {
            "question": question,
            "options": options or ["方案A", "方案B"],
            "blocking_task_keys": blocking or [],
        },
    )


def reassign(task_key: str, agent_slug: str, reason: str = "原 agent 不合适") -> tuple[str, dict[str, Any]]:
    return ("reassign", {"task_key": task_key, "agent_slug": agent_slug, "reason": reason})


def revise_plan(changes: dict[str, Any], reason: str = "需要调整") -> tuple[str, dict[str, Any]]:
    return ("revise_plan", {"changes": changes, "reason": reason})


def load_scopes_dict(*scopes: dict[str, Any]) -> dict[str, Any]:
    return {"scopes": list(scopes)}

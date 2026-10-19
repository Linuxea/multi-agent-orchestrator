"""主 agent 控制工具的 JSON Schema 定义（§5.2）。

agent_slug 参数在每次调用模型前动态生成为当前所有 active agent slug 的枚举。
每个工具都带 rationale（≤300 字，缺失即拒绝）。
"""

from __future__ import annotations

from typing import Any

from app.schemas.contracts import TaskReport
from app.schemas.plan import Review

_RATIONALE: dict[str, Any] = {
    "type": "string",
    "description": "本次决定的理由（必填，≤300 字），将写入决策记录",
    "maxLength": 300,
}


def _with_rationale(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    props = {"rationale": dict(_RATIONALE), **properties}
    req = ["rationale", *required]
    return {"type": "object", "properties": props, "required": req, "additionalProperties": False}


_TASK_SPEC_PROPS: dict[str, Any] = {
    "key": {"type": "string", "description": "运行内唯一短标识，如 T3"},
    "title": {"type": "string"},
    "goal": {"type": "string"},
    "agent_slug": {"type": "string"},
    "depends_on": {"type": "array", "items": {"type": "string"}},
    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
    "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    "optional": {"type": "boolean"},
    "playbook_stage_key": {
        "type": "string",
        "description": "对应会话编队剧本中的阶段 key（如 S1）；未绑定剧本时留空",
    },
}


def control_tool_schemas(agent_slugs: list[str]) -> list[dict[str, Any]]:
    slug_enum: dict[str, Any] = {"type": "string"}
    if agent_slugs:
        slug_enum = {"type": "string", "enum": agent_slugs}

    return [
        {
            "name": "search_agents",
            "description": (
                "按描述和标签检索注册表，返回可用（active）agent 的摘要；"
                "传 team（团队 slug）时改为返回该团队成员"
            ),
            "input_schema": _with_rationale(
                {
                    "query": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "team": {"type": "string", "description": "团队 slug：只看该团队成员"},
                },
                [],
            ),
        },
        {
            "name": "get_agent",
            "description": "读取单个 agent 的详细职责、输入输出约定、可用工具",
            "input_schema": _with_rationale({"agent_slug": slug_enum}, ["agent_slug"]),
        },
        {
            "name": "list_playbooks",
            "description": "列出团队模板；传 team（团队 slug）时只返回该团队绑定的剧本",
            "input_schema": _with_rationale({"team": {"type": "string"}}, []),
        },
        {
            "name": "get_playbook",
            "description": "读取团队模板详情",
            "input_schema": _with_rationale({"playbook_id": {"type": "string"}}, ["playbook_id"]),
        },
        {
            "name": "propose_plan",
            "description": "提交初版计划（任务依赖图）。依赖必须无环；任务数有上限",
            "input_schema": _with_rationale(
                {
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": _TASK_SPEC_PROPS,
                            "required": ["key", "title", "agent_slug", "acceptance_criteria"],
                            "additionalProperties": False,
                        },
                    },
                },
                ["tasks"],
            ),
        },
        {
            "name": "revise_plan",
            "description": "增、删、改任务或依赖，生成新计划版本。已 accepted 的任务不可删除或修改",
            "input_schema": _with_rationale(
                {
                    "changes": {
                        "type": "object",
                        "properties": {
                            "add_tasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": _TASK_SPEC_PROPS,
                                    "required": ["key", "title", "agent_slug", "acceptance_criteria"],
                                    "additionalProperties": False,
                                },
                            },
                            "remove_task_keys": {"type": "array", "items": {"type": "string"}},
                            "update_tasks": {
                                "type": "array",
                                "items": {"type": "object"},
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                    "reason": {"type": "string", "description": "计划修订原因"},
                },
                ["changes", "reason"],
            ),
        },
        {
            "name": "dispatch",
            "description": "为 ready/needs_rework/blocked 状态的任务提交任务单，进入派发队列",
            "input_schema": _with_rationale(
                {
                    "task_key": {"type": "string"},
                    "objective": {"type": "string"},
                    "context": {"type": "string", "maxLength": 3000},
                    "inputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "artifact_id": {"type": "string"},
                                "usage": {"type": "string"},
                            },
                            "required": ["artifact_id"],
                            "additionalProperties": False,
                        },
                    },
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "deliverable_format": {"type": "string"},
                    "deliverable_name": {"type": "string"},
                    "deliverable_notes": {"type": "string"},
                    "budget": {
                        "type": "object",
                        "properties": {
                            "max_turns": {"type": "integer"},
                            "max_input_tokens": {"type": "integer"},
                            "max_output_tokens": {"type": "integer"},
                            "max_cost": {"type": "number"},
                            "max_wall_seconds": {"type": "integer"},
                            "max_tool_calls": {"type": "integer"},
                        },
                        "additionalProperties": False,
                    },
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "selection_rationale": {"type": "string"},
                    "agent_slug": slug_enum,
                    "rework_feedback": {"type": "string"},
                },
                ["task_key", "objective", "acceptance_criteria", "selection_rationale", "agent_slug"],
            ),
        },
        {
            "name": "review",
            "description": (
                "提交对某次回报的复核结论。每个验收标准都必须给出结论。"
                "attempt_id 与 task_key 二选一（task_key 取该任务最近一次待复核尝试）"
            ),
            "input_schema": _with_rationale(
                {
                    "attempt_id": {"type": "string"},
                    "task_key": {"type": "string"},
                    "review": Review.model_json_schema(),
                },
                ["review"],
            ),
        },
        {
            "name": "reassign",
            "description": "改派任务给其他 agent",
            "input_schema": _with_rationale(
                {
                    "task_key": {"type": "string"},
                    "agent_slug": slug_enum,
                    "reason": {"type": "string"},
                },
                ["task_key", "agent_slug", "reason"],
            ),
        },
        {
            "name": "split",
            "description": "把一个任务拆成多个子任务；原任务变为 superseded",
            "input_schema": _with_rationale(
                {
                    "task_key": {"type": "string"},
                    "subtasks": {
                        "type": "array",
                        "minItems": 2,
                        "items": {
                            "type": "object",
                            "properties": _TASK_SPEC_PROPS,
                            "required": ["key", "title", "agent_slug", "acceptance_criteria"],
                            "additionalProperties": False,
                        },
                    },
                },
                ["task_key", "subtasks"],
            ),
        },
        {
            "name": "ask_user",
            "description": "向用户提问，可附选项；只阻塞列出的任务",
            "input_schema": _with_rationale(
                {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "blocking_task_keys": {"type": "array", "items": {"type": "string"}},
                },
                ["question"],
            ),
        },
        {
            "name": "read_artifact",
            "description": (
                "读取交付物：summary（摘要）/ full（全文，超长自动截断）/ range（区间）。"
                "artifact_id 与 task_key 二选一（task_key 取该任务最近一次提交的交付物）"
            ),
            "input_schema": _with_rationale(
                {
                    "artifact_id": {"type": "string"},
                    "task_key": {"type": "string"},
                    "mode": {"type": "string", "enum": ["summary", "full", "range"]},
                    "offset": {"type": "integer", "minimum": 0},
                    "length": {"type": "integer", "minimum": 1},
                },
                ["mode"],
            ),
        },
        {
            "name": "skip",
            "description": "放弃某个可选任务",
            "input_schema": _with_rationale(
                {"task_key": {"type": "string"}}, ["task_key"]
            ),
        },
        {
            "name": "finalize",
            "description": "提交最终答复。所有任务必须处于终态；引用必须指向存在的交付物或任务",
            "input_schema": _with_rationale(
                {
                    "answer": {"type": "string"},
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "task_key": {"type": "string"},
                                "artifact_id": {"type": "string"},
                                "marker": {"type": "string"},
                            },
                            "required": ["task_key"],
                            "additionalProperties": False,
                        },
                    },
                    "gaps": {"type": "string", "description": "未完成部分与原因"},
                },
                ["answer", "citations"],
            ),
        },
    ]


# submit_report 的 schema 供参考导出（子 agent 侧）
SUBMIT_REPORT_SCHEMA: dict[str, Any] = TaskReport.model_json_schema()

"""事件载荷 Pydantic 模型（§12.4 事件目录）。

每种事件的 payload 在此定义为 Pydantic 模型；extra="allow" 便于演进。
前端类型由导出的 JSON Schema 生成（第二期接入）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Budget, Usage


class _Payload(BaseModel):
    model_config = ConfigDict(extra="allow")


# --- 运行 ---
class RunCreatedPayload(_Payload):
    session_id: str
    trigger_message_id: str
    user_request: str


class RunStatusChangedPayload(_Payload):
    from_status: str
    to_status: str


class RunCompletedPayload(_Payload):
    outcome: str
    final_answer: str
    gaps: str
    usage: Usage


class RunFailedPayload(_Payload):
    error: str


class RunCancelledPayload(_Payload):
    reason: str = ""


# --- 主 agent ---
class OrchestratorWokePayload(_Payload):
    reasons: list[str]


class OrchestratorThinkingPayload(_Payload):
    text: str


class OrchestratorToolCalledPayload(_Payload):
    tool: str
    arguments: dict[str, object] = Field(default_factory=dict)
    rationale: str = ""
    result_summary: str = ""


class OrchestratorToolRejectedPayload(_Payload):
    tool: str
    arguments: dict[str, object] = Field(default_factory=dict)
    reason: str


class OrchestratorMessagePayload(_Payload):
    text: str


# --- 计划 ---
class PlanProposedPayload(_Payload):
    version: int
    task_count: int
    estimated_cost: float | None = None
    rationale: str = ""
    deviates_from_playbook: list[str] = Field(default_factory=list)


class PlanApprovedPayload(_Payload):
    version: int
    by: str = "user"
    modifications: dict[str, object] = Field(default_factory=dict)


class PlanRejectedPayload(_Payload):
    version: int
    note: str = ""


class PlanRevisedPayload(_Payload):
    version: int
    reason: str


# --- 任务 ---
class TaskCreatedPayload(_Payload):
    key: str
    title: str
    agent_slug: str
    depends_on: list[str] = Field(default_factory=list)


class TaskStatusChangedPayload(_Payload):
    key: str
    from_status: str
    to_status: str
    reason: str = ""


class TaskReassignedPayload(_Payload):
    key: str
    from_agent: str
    to_agent: str
    reason: str


class TaskSplitPayload(_Payload):
    key: str
    subtask_keys: list[str]
    reason: str


class TaskSkippedPayload(_Payload):
    key: str
    reason: str


# --- 尝试 ---
class AttemptQueuedPayload(_Payload):
    task_key: str
    number: int
    agent_slug: str


class AttemptStartedPayload(_Payload):
    task_key: str
    number: int
    agent_slug: str
    workspace_path: str
    tools: list[str] = Field(default_factory=list)
    model: str
    budget: Budget | None = None


class AttemptOutputDeltaPayload(_Payload):
    text: str


class AttemptOutputPayload(_Payload):
    text: str


class AttemptThinkingPayload(_Payload):
    text: str


class AttemptUsagePayload(_Payload):
    usage: Usage


class AttemptInputInjectedPayload(_Payload):
    text: str


class AttemptReportedPayload(_Payload):
    task_key: str
    number: int
    report: dict[str, object]


class AttemptTimedOutPayload(_Payload):
    task_key: str
    number: int
    error: str = ""


class AttemptCrashedPayload(_Payload):
    task_key: str
    number: int
    error: str
    last_output: str = ""


class AttemptCancelledPayload(_Payload):
    task_key: str
    number: int
    reason: str = ""


# --- 工具 ---
class ToolCallStartedPayload(_Payload):
    task_key: str = ""
    attempt_id: str = ""
    tool: str
    arguments: dict[str, object] = Field(default_factory=dict)
    permission: str = "allow"
    permission_basis: str = ""


class ToolCallFinishedPayload(_Payload):
    task_key: str = ""
    attempt_id: str = ""
    tool: str
    ok: bool = True
    duration_ms: int = 0
    result_summary: str = ""


class ToolCallBlockedPayload(_Payload):
    task_key: str = ""
    attempt_id: str = ""
    tool: str
    reason: str


# --- 复核 ---
class ReviewSubmittedPayload(_Payload):
    task_key: str
    attempt_id: str
    verdict: str
    review: dict[str, object]


# --- 决策 ---
class DecisionRecordedPayload(_Payload):
    decision_id: str
    type: str
    summary: str
    rationale: str
    made_by: str


# --- 交付物 ---
class ArtifactCreatedPayload(_Payload):
    artifact_id: str
    name: str
    kind: str
    task_key: str = ""
    attempt_id: str = ""
    summary: str = ""


class ArtifactStatusChangedPayload(_Payload):
    artifact_id: str
    from_status: str
    to_status: str


# --- 用户交互 ---
class UserQuestionAskedPayload(_Payload):
    question_id: str
    question: str
    options: list[str] = Field(default_factory=list)
    blocking_task_keys: list[str] = Field(default_factory=list)


class UserAnsweredPayload(_Payload):
    question_id: str
    answer: str


class UserIntervenedPayload(_Payload):
    action: str
    details: dict[str, object] = Field(default_factory=dict)


# --- 审批 ---
class ApprovalRequestedPayload(_Payload):
    approval_id: str
    requested_by: str
    tool_slug: str
    action: str
    risk_note: str


class ApprovalDecidedPayload(_Payload):
    approval_id: str
    approved: bool
    note: str = ""


class ApprovalExpiredPayload(_Payload):
    approval_id: str


# --- 预算 ---
class BudgetWarningPayload(_Payload):
    level: str  # attempt / run / global
    ratio: float
    usage: Usage


class BudgetExceededPayload(_Payload):
    level: str
    usage: Usage


class BudgetExtendedPayload(_Payload):
    new_budget: Budget


# --- 配置 ---
class ConfigChangedPayload(_Payload):
    entity: str
    entity_id: str
    diff: dict[str, object] = Field(default_factory=dict)


class FactoryGeneratedPayload(_Payload):
    kind: str
    input_summary: str = ""
    output_summary: str = ""


# --- 引擎 ---
class EngineInvalidTransitionPayload(_Payload):
    entity: str  # task / run / attempt
    entity_id: str
    from_status: str
    to_status: str
    actor: str


class EngineRecoveredPayload(_Payload):
    detail: str


class EngineCircuitOpenPayload(_Payload):
    detail: str


EVENT_PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    "run.created": RunCreatedPayload,
    "run.status_changed": RunStatusChangedPayload,
    "run.completed": RunCompletedPayload,
    "run.failed": RunFailedPayload,
    "run.cancelled": RunCancelledPayload,
    "orchestrator.woke": OrchestratorWokePayload,
    "orchestrator.thinking": OrchestratorThinkingPayload,
    "orchestrator.tool_called": OrchestratorToolCalledPayload,
    "orchestrator.tool_rejected": OrchestratorToolRejectedPayload,
    "orchestrator.message": OrchestratorMessagePayload,
    "plan.proposed": PlanProposedPayload,
    "plan.approved": PlanApprovedPayload,
    "plan.rejected": PlanRejectedPayload,
    "plan.revised": PlanRevisedPayload,
    "task.created": TaskCreatedPayload,
    "task.status_changed": TaskStatusChangedPayload,
    "task.reassigned": TaskReassignedPayload,
    "task.split": TaskSplitPayload,
    "task.skipped": TaskSkippedPayload,
    "attempt.queued": AttemptQueuedPayload,
    "attempt.started": AttemptStartedPayload,
    "attempt.output.delta": AttemptOutputDeltaPayload,
    "attempt.output": AttemptOutputPayload,
    "attempt.thinking": AttemptThinkingPayload,
    "attempt.usage": AttemptUsagePayload,
    "attempt.input_injected": AttemptInputInjectedPayload,
    "attempt.reported": AttemptReportedPayload,
    "attempt.timed_out": AttemptTimedOutPayload,
    "attempt.crashed": AttemptCrashedPayload,
    "attempt.cancelled": AttemptCancelledPayload,
    "tool.call.started": ToolCallStartedPayload,
    "tool.call.finished": ToolCallFinishedPayload,
    "tool.call.blocked": ToolCallBlockedPayload,
    "review.submitted": ReviewSubmittedPayload,
    "decision.recorded": DecisionRecordedPayload,
    "artifact.created": ArtifactCreatedPayload,
    "artifact.status_changed": ArtifactStatusChangedPayload,
    "user.question_asked": UserQuestionAskedPayload,
    "user.answered": UserAnsweredPayload,
    "user.intervened": UserIntervenedPayload,
    "approval.requested": ApprovalRequestedPayload,
    "approval.decided": ApprovalDecidedPayload,
    "approval.expired": ApprovalExpiredPayload,
    "budget.warning": BudgetWarningPayload,
    "budget.exceeded": BudgetExceededPayload,
    "budget.extended": BudgetExtendedPayload,
    "config.agent_changed": ConfigChangedPayload,
    "config.tool_changed": ConfigChangedPayload,
    "config.model_changed": ConfigChangedPayload,
    "config.settings_changed": ConfigChangedPayload,
    "factory.generated": FactoryGeneratedPayload,
    "engine.invalid_transition": EngineInvalidTransitionPayload,
    "engine.recovered": EngineRecoveredPayload,
    "engine.circuit_open": EngineCircuitOpenPayload,
}

PERSISTED_EVENT_TYPES: set[str] = set(EVENT_PAYLOAD_MODELS) - {"attempt.output.delta"}

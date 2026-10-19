"""全平台枚举（字段值以规格书为准，代码用英文 snake_case）。"""

from __future__ import annotations

from enum import StrEnum


class AgentStatus(StrEnum):
    draft = "draft"
    active = "active"
    disabled = "disabled"
    archived = "archived"


class AgentOrigin(StrEnum):
    manual = "manual"
    factory = "factory"
    preset = "preset"


class ExecutorKind(StrEnum):
    sdk = "sdk"
    lite = "lite"


class SessionStatus(StrEnum):
    active = "active"
    archived = "archived"


class RunStatus(StrEnum):
    created = "created"
    planning = "planning"
    clarifying = "clarifying"
    awaiting_plan_approval = "awaiting_plan_approval"
    running = "running"
    paused = "paused"
    awaiting_user = "awaiting_user"
    synthesizing = "synthesizing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.completed, RunStatus.failed, RunStatus.cancelled)


class TaskStatus(StrEnum):
    pending = "pending"
    ready = "ready"
    queued = "queued"
    running = "running"
    awaiting_approval = "awaiting_approval"
    reported = "reported"
    needs_rework = "needs_rework"
    blocked = "blocked"
    awaiting_user = "awaiting_user"
    accepted = "accepted"
    failed = "failed"
    skipped = "skipped"
    superseded = "superseded"
    cancelled = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            TaskStatus.accepted,
            TaskStatus.failed,
            TaskStatus.skipped,
            TaskStatus.superseded,
            TaskStatus.cancelled,
        )


class AttemptStatus(StrEnum):
    queued = "queued"
    running = "running"
    awaiting_approval = "awaiting_approval"
    reported = "reported"
    timed_out = "timed_out"
    crashed = "crashed"
    cancelled = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            AttemptStatus.reported,
            AttemptStatus.timed_out,
            AttemptStatus.crashed,
            AttemptStatus.cancelled,
        )


class PlanApprovalMode(StrEnum):
    always = "always"
    auto_if_under = "auto_if_under"
    never = "never"


class RunOutcome(StrEnum):
    success = "success"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"


class ReportStatus(StrEnum):
    done = "done"
    partial = "partial"
    blocked = "blocked"
    failed = "failed"


class BlockerType(StrEnum):
    missing_tool = "missing_tool"
    missing_info = "missing_info"
    out_of_scope = "out_of_scope"
    needs_collaboration = "needs_collaboration"
    needs_decision = "needs_decision"
    external_error = "external_error"
    budget_exhausted = "budget_exhausted"


class ReviewVerdict(StrEnum):
    pass_ = "pass"
    pass_with_gaps = "pass_with_gaps"
    fail = "fail"


class ArtifactKind(StrEnum):
    file = "file"
    markdown = "markdown"
    json = "json"
    link = "link"


class ArtifactStatus(StrEnum):
    draft = "draft"
    submitted = "submitted"
    accepted = "accepted"
    rejected = "rejected"


class DecisionType(StrEnum):
    plan_created = "plan_created"
    plan_revised = "plan_revised"
    agent_selected = "agent_selected"
    dispatched = "dispatched"
    review_passed = "review_passed"
    review_failed = "review_failed"
    reassigned = "reassigned"
    split_task = "split"  # 成员名避开 str.split（mypy），值不变
    asked_user = "asked_user"
    skipped = "skipped"
    retried = "retried"
    escalated = "escalated"
    budget_requested = "budget_requested"
    finalized = "finalized"


class DecisionMaker(StrEnum):
    orchestrator = "orchestrator"
    user = "user"
    rule = "rule"


class ToolKind(StrEnum):
    builtin = "builtin"
    mcp = "mcp"
    skill = "skill"
    command = "command"
    hook = "hook"
    custom_http = "custom_http"
    custom_python = "custom_python"


class ToolPermission(StrEnum):
    allow = "allow"
    ask = "ask"
    deny = "deny"


class ToolStatus(StrEnum):
    untested = "untested"
    ok = "ok"
    error = "error"
    disabled = "disabled"


class ModelProtocol(StrEnum):
    anthropic_messages = "anthropic_messages"
    openai_chat = "openai_chat"  # 预留


class ThinkingMode(StrEnum):
    off = "off"
    on = "on"


class ApprovalStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class RememberScope(StrEnum):
    once = "once"
    this_run = "this_run"
    always_for_agent = "always_for_agent"


class MessageRole(StrEnum):
    user = "user"
    orchestrator = "orchestrator"

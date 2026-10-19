"""运行侧实体：Session、Message、Run、Plan、Task、Attempt、Artifact、Decision、Approval（§4）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import (
    ApprovalStatus,
    ArtifactKind,
    ArtifactStatus,
    AttemptStatus,
    DecisionMaker,
    DecisionType,
    MessageRole,
    RememberScope,
    RunOutcome,
    RunStatus,
    SessionStatus,
    TaskStatus,
)
from app.models.base import Base, IdMixin
from app.utils import utcnow


class Session(IdMixin, Base):
    __tablename__ = "sessions"

    title: Mapped[str] = mapped_column(String(300), default="新会话")
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, native_enum=False, length=20), default=SessionStatus.active
    )
    default_team_id: Mapped[str | None] = mapped_column(String(40), default=None)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Message(IdMixin, Base):
    __tablename__ = "messages"

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, native_enum=False, length=20)
    )
    content: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    attachments: Mapped[list[str]] = mapped_column(JSON, default=list)


class Run(IdMixin, Base):
    __tablename__ = "runs"

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    trigger_message_id: Mapped[str] = mapped_column(String(40))
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, native_enum=False, length=32), default=RunStatus.created
    )
    current_plan_version: Mapped[int] = mapped_column(Integer, default=0)
    budget: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    final_answer: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[RunOutcome | None] = mapped_column(
        Enum(RunOutcome, native_enum=False, length=20), default=None
    )
    summary_of_gaps: Mapped[str] = mapped_column(Text, default="")


class Plan(IdMixin, Base):
    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("run_id", "version", name="uq_plan_run_version"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    tasks_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    change_reason: Mapped[str] = mapped_column(Text, default="")
    proposed_by: Mapped[str] = mapped_column(String(20), default="orchestrator")
    approved: Mapped[bool] = mapped_column(default=False)


class Task(IdMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_task_run_key"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    key: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(300))
    goal: Mapped[str] = mapped_column(Text, default="")
    assigned_agent_id: Mapped[str] = mapped_column(String(40))
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, native_enum=False, length=32), default=TaskStatus.pending
    )
    priority: Mapped[int] = mapped_column(Integer, default=3)
    playbook_stage_key: Mapped[str | None] = mapped_column(String(100), default=None)
    parent_task_key: Mapped[str | None] = mapped_column(String(50), default=None)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    result_artifact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    optional: Mapped[bool] = mapped_column(default=False)


class Attempt(IdMixin, Base):
    __tablename__ = "attempts"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    agent_id: Mapped[str] = mapped_column(String(40))
    agent_version: Mapped[int] = mapped_column(Integer, default=1)
    brief: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[AttemptStatus] = mapped_column(
        Enum(AttemptStatus, native_enum=False, length=32), default=AttemptStatus.queued
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    review: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    workspace_path: Mapped[str] = mapped_column(String(500), default="")
    executor_session_id: Mapped[str | None] = mapped_column(String(100), default=None)
    error: Mapped[str] = mapped_column(Text, default="")
    counted: Mapped[bool] = mapped_column(default=True)  # 是否计入 attempt_count（D-1.5）
    auto_retry_of: Mapped[str | None] = mapped_column(String(40), default=None)


class Artifact(IdMixin, Base):
    __tablename__ = "artifacts"

    run_id: Mapped[str] = mapped_column(String(40), index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(40), default=None)
    name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[ArtifactKind] = mapped_column(
        Enum(ArtifactKind, native_enum=False, length=20), default=ArtifactKind.file
    )
    mime: Mapped[str] = mapped_column(String(100), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    storage_path: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ArtifactStatus] = mapped_column(
        Enum(ArtifactStatus, native_enum=False, length=20),
        default=ArtifactStatus.draft,
    )
    derived_from: Mapped[list[str]] = mapped_column(JSON, default=list)


class Decision(IdMixin, Base):
    __tablename__ = "decisions"

    run_id: Mapped[str] = mapped_column(String(40), index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    type: Mapped[DecisionType] = mapped_column(
        Enum(DecisionType, native_enum=False, length=30)
    )
    summary: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")
    alternatives: Mapped[list[str]] = mapped_column(JSON, default=list)
    inputs: Mapped[list[str]] = mapped_column(JSON, default=list)
    made_by: Mapped[DecisionMaker] = mapped_column(
        Enum(DecisionMaker, native_enum=False, length=20),
        default=DecisionMaker.orchestrator,
    )


class Approval(IdMixin, Base):
    __tablename__ = "approvals"

    run_id: Mapped[str] = mapped_column(String(40), index=True)
    attempt_id: Mapped[str] = mapped_column(String(40), index=True)
    requested_by: Mapped[str] = mapped_column(String(100))
    tool_slug: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(200))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    risk_note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, native_enum=False, length=20),
        default=ApprovalStatus.pending,
    )
    decision_note: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    remember: Mapped[RememberScope | None] = mapped_column(
        Enum(RememberScope, native_enum=False, length=30), default=None
    )


class Event(Base):
    """事件（§4.15）：只追加、不修改、不删除。"""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_run_seq", "run_id", "seq"),
        Index("ix_events_type", "type"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default="")
    run_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    seq: Mapped[int | None] = mapped_column(Integer, default=None)
    type: Mapped[str] = mapped_column(String(60))
    actor: Mapped[str] = mapped_column(String(100))
    task_key: Mapped[str | None] = mapped_column(String(50), default=None)
    attempt_id: Mapped[str | None] = mapped_column(String(40), default=None)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    """全局设置（§13.1），单行 JSON。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UsageLedger(Base):
    """费用/耗时分项账本（run.usage 的分 agent 明细来源）。"""

    __tablename__ = "usage_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    task_key: Mapped[str] = mapped_column(String(50), default="")
    agent_slug: Mapped[str] = mapped_column(String(100), default="")
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    wall_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

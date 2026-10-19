"""注册表侧实体：Agent、AgentVersion、Team、Playbook、ModelProfile、Secret、Tool（§4）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import (
    AgentOrigin,
    AgentStatus,
    ExecutorKind,
    ModelProtocol,
    ThinkingMode,
    ToolKind,
    ToolPermission,
    ToolStatus,
)
from app.models.base import Base, IdMixin


class Agent(IdMixin, Base):
    __tablename__ = "agents"

    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500))
    role_detail: Mapped[str] = mapped_column(Text, default="")
    capability_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    system_prompt: Mapped[str] = mapped_column(Text)
    executor: Mapped[ExecutorKind] = mapped_column(
        Enum(ExecutorKind, native_enum=False, length=20)
    )
    model_profile_id: Mapped[str] = mapped_column(String(40))
    input_contract: Mapped[str] = mapped_column(Text, default="")
    output_contract: Mapped[str] = mapped_column(Text, default="")
    default_budget: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, native_enum=False, length=20), default=AgentStatus.draft
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    origin: Mapped[AgentOrigin] = mapped_column(
        Enum(AgentOrigin, native_enum=False, length=20), default=AgentOrigin.manual
    )
    avatar: Mapped[str | None] = mapped_column(String(20), default=None)
    a2a_card: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)


class AgentVersion(IdMixin, Base):
    __tablename__ = "agent_versions"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    change_note: Mapped[str] = mapped_column(Text, default="")


class Team(IdMixin, Base):
    __tablename__ = "teams"

    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500), default="")
    member_agent_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    playbook_id: Mapped[str | None] = mapped_column(String(40), default=None)
    lead_hint: Mapped[str] = mapped_column(Text, default="")


class Playbook(IdMixin, Base):
    __tablename__ = "playbooks"

    name: Mapped[str] = mapped_column(String(200))
    trigger_hint: Mapped[str] = mapped_column(Text, default="")
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class ModelProfile(IdMixin, Base):
    __tablename__ = "model_profiles"

    name: Mapped[str] = mapped_column(String(200))
    protocol: Mapped[ModelProtocol] = mapped_column(
        Enum(ModelProtocol, native_enum=False, length=40),
        default=ModelProtocol.anthropic_messages,
    )
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_secret_id: Mapped[str | None] = mapped_column(String(40), default=None)
    model: Mapped[str] = mapped_column(String(200))
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=8192)
    temperature: Mapped[float | None] = mapped_column(Float, default=None)
    thinking: Mapped[ThinkingMode] = mapped_column(
        Enum(ThinkingMode, native_enum=False, length=10), default=ThinkingMode.off
    )
    context_window: Mapped[int] = mapped_column(Integer, default=128000)
    price_input: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    price_output: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    is_default_orchestrator: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default_worker: Mapped[bool] = mapped_column(Boolean, default=False)


class Secret(IdMixin, Base):
    __tablename__ = "secrets"

    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    encrypted_value: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    last_used_at: Mapped[str | None] = mapped_column(default=None)


class Tool(IdMixin, Base):
    __tablename__ = "tools"

    kind: Mapped[ToolKind] = mapped_column(Enum(ToolKind, native_enum=False, length=30))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    default_permission: Mapped[ToolPermission] = mapped_column(
        Enum(ToolPermission, native_enum=False, length=10), default=ToolPermission.allow
    )
    status: Mapped[ToolStatus] = mapped_column(
        Enum(ToolStatus, native_enum=False, length=20), default=ToolStatus.untested
    )
    last_test: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    discovered: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)


class ToolBinding(IdMixin, Base):
    __tablename__ = "tool_bindings"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    tool_id: Mapped[str] = mapped_column(ForeignKey("tools.id"), index=True)
    enabled_subtools: Mapped[list[str]] = mapped_column(JSON, default=list)
    permission: Mapped[ToolPermission | None] = mapped_column(
        Enum(ToolPermission, native_enum=False, length=10), default=None
    )
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

"""派发契约：任务单 TaskBrief（§5.3）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.enums import BlockerType, ReportStatus
from app.schemas.common import Budget


class ArtifactInput(BaseModel):
    """任务单中的输入交付物引用。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    usage: str = ""


class DeliverableSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = "markdown"
    name: str = ""
    notes: str = ""


class EntryCommand(BaseModel):
    """§8.5：任务单可指定 agent 级 Command 作为工作入口。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: str = ""


class TaskBrief(BaseModel):
    """主 agent → 子 agent 的结构化任务单。"""

    model_config = ConfigDict(extra="forbid")

    task_key: str
    agent_slug: str
    objective: str
    context: str = ""
    inputs: list[ArtifactInput] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(min_length=1)
    deliverable: DeliverableSpec
    budget: Budget | None = None
    priority: int = 3
    selection_rationale: str
    rework_of: str | None = None
    rework_feedback: str | None = None
    entry_command: EntryCommand | None = None


class Blocker(BaseModel):
    """受阻原因（§5.4 blocker.type 枚举）。"""

    model_config = ConfigDict(extra="forbid")

    type: BlockerType
    detail: str
    needs: str = ""


class ReportDeliverable(BaseModel):
    """回报中的交付物：lite 执行器允许内联内容（§9.8），sdk 用路径。"""

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    name: str | None = None
    content: str | None = None
    summary: str = ""


class CriteriaSelfCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: str
    met: bool
    note: str = ""


class TaskReport(BaseModel):
    """子 agent → 主 agent 的结构化回报。"""

    model_config = ConfigDict(extra="forbid")

    status: ReportStatus
    summary: str = ""
    deliverables: list[ReportDeliverable] = Field(default_factory=list)
    criteria_self_check: list[CriteriaSelfCheck] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    blocker: Blocker | None = None
    suggestions: list[str] = Field(default_factory=list)
    attempted: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _blocker_required(self) -> TaskReport:
        if self.status in (ReportStatus.blocked, ReportStatus.failed) and self.blocker is None:
            raise ValueError("blocked/failed 回报必须附 blocker")
        return self

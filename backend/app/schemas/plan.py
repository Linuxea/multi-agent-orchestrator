"""复核契约 Review（§5.9）与计划/任务快照结构。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.enums import ReviewVerdict


class CriterionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: str
    result: ReviewVerdict
    comment: str = ""


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: ReviewVerdict
    criteria: list[CriterionResult] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    optional_suggestions: list[str] = Field(default_factory=list)
    gaps_note: str = ""  # 采纳 partial 时必填（§5.7 硬性约束）
    reviewer: str = "orchestrator"


class PlanTaskSpec(BaseModel):
    """propose_plan / revise_plan 中的任务定义。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    goal: str = ""
    agent_slug: str
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 3
    playbook_stage_key: str | None = None
    acceptance_criteria: list[str] = Field(min_length=1)
    optional: bool = False


class PlanSnapshot(BaseModel):
    """计划版本的任务快照（§4.11 tasks_snapshot）。"""

    model_config = ConfigDict(extra="forbid")

    tasks: list[PlanTaskSpec] = Field(default_factory=list)

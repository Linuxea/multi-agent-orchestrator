"""Pydantic 契约：预算、用量与公共模型（§4.12）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Budget(BaseModel):
    """预算：任一字段为 None 表示不限；运行级必须设置 max_cost 与 max_wall_seconds。"""

    model_config = ConfigDict(extra="forbid")

    max_turns: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost: float | None = None
    max_wall_seconds: int | None = None
    max_tool_calls: int | None = None


class Usage(BaseModel):
    """用量累计。"""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0
    wall_seconds: float = 0.0
    turns: int = 0
    tool_calls: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cost += other.cost
        self.wall_seconds += other.wall_seconds
        self.turns += other.turns
        self.tool_calls += other.tool_calls

    def ratio(self, budget: Budget) -> float | None:
        """相对预算的使用率（取已设字段中的最大值）。"""
        ratios: list[float] = []
        pairs: list[tuple[float | None, float]] = [
            (budget.max_turns, self.turns),
            (budget.max_input_tokens, self.input_tokens),
            (budget.max_output_tokens, self.output_tokens),
            (budget.max_cost, self.cost),
            (budget.max_wall_seconds, self.wall_seconds),
            (budget.max_tool_calls, self.tool_calls),
        ]
        for limit, value in pairs:
            if limit is not None and limit > 0:
                ratios.append(value / limit)
        return max(ratios) if ratios else None


DEFAULT_TASK_BUDGET = Budget(max_turns=24, max_cost=1.0, max_wall_seconds=1800)


class Page(BaseModel):
    """游标分页（§12.1）。"""

    items: list[dict[str, object]] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class ApiError(BaseModel):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)

"""运行时公共类型：模型配置、执行事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.common import Budget, Usage


@dataclass
class ModelConfig:
    """从 ModelProfile 解析出的可直接使用的模型配置（密钥已解密）。"""

    profile_id: str
    model: str
    base_url: str
    api_key: str
    max_output_tokens: int = 8192
    temperature: float | None = None
    thinking: str = "off"
    context_window: int = 128000
    price_input: float = 0.0
    price_output: float = 0.0

    def cost_of(self, usage: Usage) -> float:
        return (
            usage.input_tokens * float(self.price_input)
            + usage.output_tokens * float(self.price_output)
        ) / 1_000_000


@dataclass
class ContentBlock:
    """模型响应内容块的规范化表示。"""

    type: str  # text | thinking | tool_use
    text: str = ""
    tool_id: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    blocks: list[ContentBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)


# --- 执行器事件（ExecEvent）：运行时把执行过程翻译为平台事件（§9.7）---
@dataclass
class EvOutputDelta:
    text: str


@dataclass
class EvOutputTurn:
    text: str


@dataclass
class EvThinking:
    text: str


@dataclass
class EvUsage:
    usage: Usage


@dataclass
class EvToolStarted:
    tool: str
    arguments: dict[str, Any]


@dataclass
class EvToolFinished:
    tool: str
    ok: bool
    duration_ms: int
    result_summary: str


@dataclass
class EvReported:
    report: dict[str, Any]


@dataclass
class EvCrashed:
    error: str
    last_output: str = ""


@dataclass
class EvTimedOut:
    error: str = ""


ExecEvent = (
    EvOutputDelta
    | EvOutputTurn
    | EvThinking
    | EvUsage
    | EvToolStarted
    | EvToolFinished
    | EvReported
    | EvCrashed
    | EvTimedOut
)


@dataclass
class RunContext:
    """执行一次尝试所需的全部已物化上下文。"""

    run_id: str
    task_key: str
    attempt_id: str
    attempt_no: int
    agent_slug: str
    workspace: str
    system_prompt: str
    first_input: str
    budget: Budget
    model: ModelConfig
    executor: str = "lite"
    entry_command: str | None = None
    pause_event: object | None = None  # asyncio.Event；set=允许继续（暂停门）

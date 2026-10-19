"""工作目录物化单元测试（§9.2、§9.3、§9.4）。"""

from __future__ import annotations

from pathlib import Path

from app.runtime.types import ModelConfig
from app.runtime.workspace import (
    assemble_system_prompt,
    build_workspace,
    first_user_input,
    workspace_root,
)
from app.schemas.common import Budget
from app.schemas.contracts import DeliverableSpec, TaskBrief


def _brief(**overrides: object) -> TaskBrief:
    data: dict[str, object] = {
        "task_key": "T1",
        "agent_slug": "researcher",
        "objective": "调研测试",
        "context": "背景说明",
        "acceptance_criteria": ["标准一"],
        "deliverable": DeliverableSpec(format="markdown", name="report.md"),
        "selection_rationale": "测试",
    }
    data.update(overrides)
    return TaskBrief(**data)  # type: ignore[arg-type]


def test_workspace_layout(env: None, tmp_path: Path) -> None:
    brief = _brief()
    root = build_workspace(
        run_id="run_x", task_key="T1", attempt_no=2, brief=brief, input_artifacts=[]
    )
    assert root == workspace_root("run_x", "T1", 2)
    assert (root / "inputs").is_dir()
    assert (root / "out").is_dir()
    assert (root / "work").is_dir()
    assert (root / "TASK.md").exists()
    content = (root / "TASK.md").read_text(encoding="utf-8")
    assert "调研测试" in content
    assert "标准一" in content


def test_system_prompt_sections(env: None) -> None:
    brief = _brief()
    prompt = assemble_system_prompt("你是调研员。标记词：researcher", brief, "- 输入A：上游调研")
    # §9.4 顺序：平台前言 → agent 提示词 → 契约说明 → 任务单
    assert prompt.index("执行型子 agent") < prompt.index("标记词：researcher")
    assert prompt.index("标记词：researcher") < prompt.index("平台契约（必须遵守）")
    assert prompt.index("平台契约（必须遵守）") < prompt.index("任务 T1")
    assert "不可信数据" in prompt
    assert "inputs" in prompt and "out/" in prompt


def test_first_user_input(env: None) -> None:
    assert first_user_input(_brief()) == "请开始执行任务"
    brief = _brief(entry_command={"name": "deep-dive", "arguments": "主题X"})
    assert first_user_input(brief) == "/deep-dive 主题X"


def test_rework_feedback_in_task_md(env: None) -> None:
    brief = _brief(rework_of="att_1", rework_feedback="必须补充两个产品")
    root = build_workspace(
        run_id="run_y", task_key="T1", attempt_no=2, brief=brief, input_artifacts=[]
    )
    content = (root / "TASK.md").read_text(encoding="utf-8")
    assert "必须补充两个产品" in content


def _model_config() -> ModelConfig:
    return ModelConfig(
        profile_id="mp", model="m", base_url="http://localhost:1", api_key="k"
    )


def test_budget_clamp_smoke(env: None) -> None:
    # 占位：预算钳制在引擎 dispatch 中测试；这里仅确认 Budget 默认值可用
    b = Budget()
    assert b.max_turns is None

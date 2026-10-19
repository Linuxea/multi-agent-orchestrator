"""状态机单元测试（§14.2 验收：每一条合法转换有测试；非法转换被拒绝）。"""

from __future__ import annotations

import pytest

from app.enums import RunStatus, TaskStatus
from app.orchestrator.states import (
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    InvalidTransition,
    check_run_transition,
    check_task_transition,
)


class TestTaskStateMachine:
    def test_every_documented_transition_is_legal(self) -> None:
        """§5.5 转换表逐行覆盖。"""
        documented = [
            ("pending", "ready"),
            ("pending", "blocked"),  # D-1.2：上游 failed/cancelled
            ("ready", "queued"),
            ("needs_rework", "queued"),
            ("blocked", "queued"),
            ("queued", "running"),
            ("running", "awaiting_approval"),
            ("awaiting_approval", "running"),
            ("running", "reported"),
            ("running", "blocked"),
            ("reported", "accepted"),
            ("reported", "needs_rework"),
            ("reported", "failed"),
            ("blocked", "failed"),
            ("blocked", "awaiting_user"),
            ("reported", "awaiting_user"),
            ("awaiting_user", "ready"),
            ("ready", "awaiting_user"),
            ("needs_rework", "awaiting_user"),
        ]
        for src, dst in documented:
            check_task_transition(TaskStatus(src), TaskStatus(dst))

    def test_every_nonterminal_can_be_superseded_skipped_cancelled(self) -> None:
        for status in TaskStatus:
            if status.is_terminal:
                continue
            for target in ("superseded", "skipped", "cancelled"):
                assert can_(status, target), f"{status.value} → {target} 应合法"

    def test_terminal_states_have_no_outgoing(self) -> None:
        for status in TaskStatus:
            if status.is_terminal:
                assert len(TASK_TRANSITIONS[status]) == 0

    def test_invalid_transitions_raise(self) -> None:
        invalid = [
            ("pending", "running"),
            ("pending", "accepted"),
            ("queued", "reported"),
            ("ready", "running"),
            ("accepted", "needs_rework"),
            ("failed", "ready"),
            ("cancelled", "queued"),
            ("superseded", "pending"),
            ("awaiting_user", "queued"),
            ("skipped", "accepted"),
        ]
        for src, dst in invalid:
            with pytest.raises(InvalidTransition):
                check_task_transition(TaskStatus(src), TaskStatus(dst))

    def test_table_is_total(self) -> None:
        assert set(TASK_TRANSITIONS) == set(TaskStatus)


class TestRunStateMachine:
    def test_documented_run_transitions(self) -> None:
        documented = [
            ("created", "planning"),
            ("planning", "clarifying"),
            ("planning", "awaiting_plan_approval"),
            ("planning", "running"),
            ("planning", "failed"),
            ("clarifying", "planning"),
            ("awaiting_plan_approval", "running"),
            ("awaiting_plan_approval", "planning"),
            ("running", "paused"),
            ("running", "awaiting_user"),
            ("running", "synthesizing"),
            ("running", "failed"),
            ("paused", "running"),
            ("awaiting_user", "running"),
            ("awaiting_user", "synthesizing"),
            ("synthesizing", "completed"),
            ("synthesizing", "failed"),
        ]
        for src, dst in documented:
            check_run_transition(RunStatus(src), RunStatus(dst))

    def test_every_nonterminal_can_cancel(self) -> None:
        for status in RunStatus:
            if status.is_terminal:
                continue
            assert can_run(status, "cancelled"), f"{status.value} → cancelled 应合法"

    def test_invalid_run_transitions(self) -> None:
        invalid = [
            ("created", "running"),
            ("completed", "running"),
            ("planning", "completed"),
            ("clarifying", "synthesizing"),
            ("synthesizing", "planning"),
            ("paused", "synthesizing"),
        ]
        for src, dst in invalid:
            with pytest.raises(InvalidTransition):
                check_run_transition(RunStatus(src), RunStatus(dst))

    def test_table_is_total(self) -> None:
        assert set(RUN_TRANSITIONS) == set(RunStatus)


def can_(src: TaskStatus, dst: str) -> bool:
    return TaskStatus(dst) in TASK_TRANSITIONS[src]


def can_run(src: RunStatus, dst: str) -> bool:
    return RunStatus(dst) in RUN_TRANSITIONS[src]

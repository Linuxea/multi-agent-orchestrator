"""任务与运行的状态机（§5.5、§5.6）。

必须严格实现：非法转换抛 InvalidTransition，调用方负责记录 engine.invalid_transition。
"""

from __future__ import annotations

from app.enums import RunStatus, TaskStatus

# 非终态任务可被 split/skip/cancel 打断（§5.5 后四行）
_CATCHALL = {
    TaskStatus.superseded,
    TaskStatus.skipped,
    TaskStatus.cancelled,
}

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    # pending → ready（依赖满足）；pending → blocked（上游 failed/cancelled，D-1.2）
    TaskStatus.pending: frozenset({TaskStatus.ready, TaskStatus.blocked}) | _CATCHALL,
    # ready/needs_rework → queued（dispatch；blocked → queued 见 D-1.1）
    TaskStatus.ready: frozenset({TaskStatus.queued, TaskStatus.awaiting_user}) | _CATCHALL,
    TaskStatus.needs_rework: frozenset({TaskStatus.queued, TaskStatus.awaiting_user}) | _CATCHALL,
    TaskStatus.queued: frozenset({TaskStatus.running}) | _CATCHALL,
    TaskStatus.running: frozenset(
        {TaskStatus.awaiting_approval, TaskStatus.reported, TaskStatus.blocked}
    )
    | _CATCHALL,
    TaskStatus.awaiting_approval: frozenset({TaskStatus.running}) | _CATCHALL,
    TaskStatus.reported: frozenset(
        {
            TaskStatus.accepted,
            TaskStatus.needs_rework,
            TaskStatus.failed,
            TaskStatus.awaiting_user,
        }
    )
    | _CATCHALL,
    TaskStatus.blocked: frozenset(
        {TaskStatus.queued, TaskStatus.failed, TaskStatus.awaiting_user}
    )
    | _CATCHALL,
    TaskStatus.awaiting_user: frozenset({TaskStatus.ready}) | _CATCHALL,
    # 终态
    TaskStatus.accepted: frozenset(),
    TaskStatus.failed: frozenset(),
    TaskStatus.skipped: frozenset(),
    TaskStatus.superseded: frozenset(),
    TaskStatus.cancelled: frozenset(),
}

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.created: frozenset({RunStatus.planning, RunStatus.cancelled}),
    RunStatus.planning: frozenset(
        {
            RunStatus.clarifying,
            RunStatus.awaiting_plan_approval,
            RunStatus.running,
            RunStatus.completed,  # 无任务需求（咨询/寒暄/注册表查询）直接 finalize 答复
            RunStatus.failed,
            RunStatus.cancelled,
        }
    ),
    RunStatus.clarifying: frozenset(
        {RunStatus.planning, RunStatus.cancelled}
    ),
    RunStatus.awaiting_plan_approval: frozenset(
        {RunStatus.running, RunStatus.planning, RunStatus.cancelled}
    ),
    RunStatus.running: frozenset(
        {
            RunStatus.paused,
            RunStatus.awaiting_user,
            RunStatus.synthesizing,
            RunStatus.failed,
            RunStatus.cancelled,
        }
    ),
    RunStatus.paused: frozenset({RunStatus.running, RunStatus.cancelled}),
    RunStatus.awaiting_user: frozenset(
        {RunStatus.running, RunStatus.synthesizing, RunStatus.cancelled}
    ),
    RunStatus.synthesizing: frozenset(
        {RunStatus.completed, RunStatus.failed, RunStatus.cancelled}
    ),
    RunStatus.completed: frozenset(),
    RunStatus.failed: frozenset(),
    RunStatus.cancelled: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, entity: str, entity_id: str, current: str, target: str) -> None:
        self.entity = entity
        self.entity_id = entity_id
        self.current = current
        self.target = target
        super().__init__(f"{entity} {entity_id}: 非法状态转换 {current} → {target}")


def check_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in TASK_TRANSITIONS[current]:
        raise InvalidTransition("task", "", current.value, target.value)


def check_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidTransition("run", "", current.value, target.value)


def can_task_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in TASK_TRANSITIONS[current]

"""工具审批门（§11、§5.6）：ask 权限的调用 → 审批单 → 等待用户决定。

- 请求：创建 Approval、发 approval.requested、任务/尝试转 awaiting_approval；
- 决定：API 调 resolve() 唤醒等待方，按 remember 记忆（once/this_run/always_for_agent）；
- 超时：approval_timeout_minutes 到期 → expired → 按拒绝处理并恢复运行。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.enums import ApprovalStatus, AttemptStatus, RememberScope, TaskStatus
from app.events.service import EventService
from app.ids import new_id
from app.models import Approval, Attempt, Task
from app.orchestrator.helpers import db_session, transition_task
from app.orchestrator.states import can_task_transition
from app.settings import SettingsService
from app.utils import utcnow

logger = logging.getLogger(__name__)


class ApprovalGate:
    def __init__(self, events: EventService, settings: SettingsService) -> None:
        self.events = events
        self.settings = settings
        self._waiters: dict[str, asyncio.Future[tuple[bool, str]]] = {}
        # 运行级记忆：approve(remember=this_run) 后同 (run, agent, tool) 自动放行
        self._run_allows: set[tuple[str, str, str]] = set()

    # ------------------------------------------------------------------
    # 记忆查询（权限链的 remembered 参数来源）
    # ------------------------------------------------------------------
    def remembers(self, run_id: str, agent_id: str, tool_slug: str) -> bool:
        return (run_id, agent_id, tool_slug) in self._run_allows

    def forget_run(self, run_id: str) -> None:
        for key in [k for k in self._run_allows if k[0] == run_id]:
            self._run_allows.discard(key)

    # ------------------------------------------------------------------
    # 请求审批（执行器工具门调用）
    # ------------------------------------------------------------------
    async def request(
        self,
        ctx: Any,  # RunContext（避免循环导入用 duck type）
        *,
        tool_slug: str,
        tool_name: str,
        action: str,
        arguments_masked: dict[str, Any],
        reason: str,
        risk_note: str,
    ) -> tuple[bool, str]:
        approval_id = new_id("apr")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, str]] = loop.create_future()
        self._waiters[approval_id] = future

        async with db_session() as session:
            approval = Approval(
                id=approval_id,
                run_id=ctx.run_id,
                attempt_id=ctx.attempt_id,
                requested_by=f"agent:{ctx.agent_slug}",
                tool_slug=tool_slug,
                action=action or tool_name,
                arguments=arguments_masked,
                reason=reason,
                risk_note=risk_note,
            )
            session.add(approval)
            attempt = await session.get(Attempt, ctx.attempt_id)
            if attempt is not None and attempt.status == AttemptStatus.running:
                attempt.status = AttemptStatus.awaiting_approval
            task = await session.get(Task, attempt.task_id) if attempt else None
            if task is not None and can_task_transition(
                TaskStatus(task.status.value), TaskStatus.awaiting_approval
            ):
                await transition_task(
                    session,
                    self.events,
                    task,
                    TaskStatus.awaiting_approval,
                    reason=f"等待工具审批: {tool_name}",
                    actor="system",
                )
            await self.events.emit(
                session,
                type="approval.requested",
                actor=f"agent:{ctx.agent_slug}",
                run_id=ctx.run_id,
                task_key=ctx.task_key,
                attempt_id=ctx.attempt_id,
                payload={
                    "approval_id": approval_id,
                    "requested_by": f"agent:{ctx.agent_slug}",
                    "tool_slug": tool_slug,
                    "action": action or tool_name,
                    "risk_note": risk_note,
                },
            )

        async with db_session() as session:
            settings = await self.settings.get(session)
        timeout_minutes = float(settings.get("approval_timeout_minutes") or 30)
        timeout_seconds = max(timeout_minutes * 60.0, 0.05)

        approved: bool
        note: str
        try:
            approved, note = await asyncio.wait_for(future, timeout_seconds)
        except TimeoutError:
            approved, note = False, "审批超时未处理，按拒绝处理"
            await self._mark_expired(approval_id)
        finally:
            self._waiters.pop(approval_id, None)

        await self._restore_running(ctx, approved, tool_name)
        return approved, note

    async def _restore_running(self, ctx: Any, approved: bool, tool_name: str) -> None:
        async with db_session() as session:
            attempt = await session.get(Attempt, ctx.attempt_id)
            if attempt is not None and attempt.status == AttemptStatus.awaiting_approval:
                attempt.status = AttemptStatus.running
            task = await session.get(Task, attempt.task_id) if attempt else None
            if (
                task is not None
                and task.status == TaskStatus.awaiting_approval
                and can_task_transition(TaskStatus.awaiting_approval, TaskStatus.running)
            ):
                await transition_task(
                    session,
                    self.events,
                    task,
                    TaskStatus.running,
                    reason=f"审批{'通过' if approved else '拒绝'}: {tool_name}",
                    actor="user",
                )

    # ------------------------------------------------------------------
    # 用户决定（API approvals/decide 调用）
    # ------------------------------------------------------------------
    async def resolve(
        self,
        approval_id: str,
        *,
        approved: bool,
        note: str,
        remember: RememberScope,
    ) -> None:
        waiter = self._waiters.get(approval_id)
        if waiter is None or waiter.done():
            return

        if approved and remember == RememberScope.this_run:
            await self._apply_remember(approval_id)
        if approved and remember == RememberScope.always_for_agent:
            await self._persist_allow(approval_id)

        if not waiter.done():
            waiter.set_result((approved, note))

    async def _apply_remember(self, approval_id: str) -> None:
        """this_run：找到 (run_id, agent_id, tool_slug) 放入运行级记忆。"""
        async with db_session() as session:
            approval = await session.get(Approval, approval_id)
            if approval is None:
                return
            attempt = await session.get(Attempt, approval.attempt_id)
            if attempt is None:
                return
            self._run_allows.add((approval.run_id, attempt.agent_id, approval.tool_slug))

    async def _persist_allow(self, approval_id: str) -> None:
        """always_for_agent：把该 agent 对该工具的绑定权限持久化为 allow。"""
        from sqlalchemy import select

        from app.enums import ToolPermission
        from app.models import Tool, ToolBinding

        async with db_session() as session:
            approval = await session.get(Approval, approval_id)
            if approval is None:
                return
            attempt = await session.get(Attempt, approval.attempt_id)
            if attempt is None:
                return
            result = await session.execute(select(Tool).where(Tool.slug == approval.tool_slug))
            tool = result.scalar_one_or_none()
            if tool is None:
                return
            bresult = await session.execute(
                select(ToolBinding).where(
                    ToolBinding.agent_id == attempt.agent_id,
                    ToolBinding.tool_id == tool.id,
                )
            )
            binding = bresult.scalar_one_or_none()
            if binding is None:
                binding = ToolBinding(
                    id=new_id("tb"),
                    agent_id=attempt.agent_id,
                    tool_id=tool.id,
                    enabled_subtools=[],
                    scope={},
                )
                session.add(binding)
            binding.permission = ToolPermission.allow
            await session.flush()

    async def _mark_expired(self, approval_id: str) -> None:
        try:
            async with db_session() as session:
                approval = await session.get(Approval, approval_id)
                if approval is None or approval.status != ApprovalStatus.pending:
                    return
                approval.status = ApprovalStatus.expired
                approval.decided_at = utcnow()
                await self.events.emit(
                    session,
                    type="approval.expired",
                    actor="system",
                    run_id=approval.run_id,
                    payload={"approval_id": approval_id},
                )
        except Exception:  # noqa: BLE001
            logger.exception("审批过期标记失败 %s", approval_id)

    # ------------------------------------------------------------------
    def pending_count(self) -> int:
        return len(self._waiters)

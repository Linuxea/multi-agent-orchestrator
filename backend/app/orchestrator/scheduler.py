"""调度器（§5.10）：并行上限、队列排序、尝试执行与事件翻译。

- 三级并行上限：全局（默认 6）、每运行（默认 4）、每 agent（max_concurrency），取最严格。
- 队列排序：优先级 → 进入 queued 的时间。
- 尝试事件翻译为平台事件（§9.7）。
- 崩溃/超时自动重试一次；external_error 延迟自动重试（均不计入尝试次数，§5.7）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.approvals import ApprovalGate
from app.config import get_config
from app.enums import AgentStatus, AttemptStatus, BlockerType, RunStatus, TaskStatus
from app.events.service import EventService
from app.ids import new_id
from app.models import Agent, Artifact, Attempt, ModelProfile, Run, Secret, Task
from app.orchestrator.helpers import db_session, record_decision, transition_task
from app.registry.service import RegistryService
from app.runtime.executor import ExecutorRegistry
from app.runtime.types import (
    EvCrashed,
    EvOutputDelta,
    EvOutputTurn,
    EvReported,
    EvThinking,
    EvTimedOut,
    EvToolFinished,
    EvToolStarted,
    EvUsage,
    GateResult,
    RunContext,
)
from app.runtime.workspace import assemble_system_prompt, build_workspace, first_user_input
from app.schemas.common import Budget, Usage
from app.schemas.contracts import TaskBrief
from app.settings import SettingsService
from app.tools.permission import check_tool_call
from app.tools.secrets_runtime import SecretMasker
from app.tools.service import ToolService
from app.utils import utcnow

if TYPE_CHECKING:
    from app.orchestrator.engine import CoordinationEngine

logger = logging.getLogger(__name__)


def _external_error_retry_delay() -> float:
    return float(getattr(get_config(), "external_error_retry_delay", 30.0))


class Scheduler:
    def __init__(
        self,
        events: EventService,
        registry: RegistryService,
        executors: ExecutorRegistry,
        settings: SettingsService,
        artifacts_repo: Any,
    ) -> None:
        self.events = events
        self.registry = registry
        self.executors = executors
        self.settings = settings
        self.artifacts = artifacts_repo
        self.engine: CoordinationEngine | None = None
        self.approvals = ApprovalGate(events, settings)
        self.tools = ToolService()

        self._running: dict[str, tuple[asyncio.Task[None], str]] = {}  # attempt_id → (task, run_id)
        self._pause_events: dict[str, asyncio.Event] = {}  # run_id → gate（set=允许执行）
        self._retry_timers: dict[str, asyncio.Task[None]] = {}  # task_id → 定时重试
        self._schedule_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def has_running(self, run_id: str | None = None) -> bool:
        live = [(t, rid) for t, rid in self._running.values() if not t.done()]
        if run_id is None:
            return bool(live)
        return any(rid == run_id for _, rid in live)

    def running_for_run(self, run_id: str) -> int:
        return sum(1 for t, rid in self._running.values() if not t.done() and rid == run_id)

    def has_pending_retry(self, run_id: str | None = None) -> bool:
        # 简化：任何未完成的定时重试都视为忙碌（按 task 维度登记，run 维度查询少见）
        return any(not t.done() for t in self._retry_timers.values())

    # ------------------------------------------------------------------
    # 暂停 / 恢复 / 中断
    # ------------------------------------------------------------------
    async def pause_run(self, run_id: str) -> None:
        gate = self._pause_events.get(run_id)
        if gate is not None:
            gate.clear()

    async def resume_run(self, run_id: str) -> None:
        gate = self._pause_events.setdefault(run_id, asyncio.Event())
        gate.set()

    async def interrupt_run(self, run_id: str) -> None:
        for attempt_id, (task, rid) in list(self._running.items()):
            if rid == run_id and not task.done():
                task.cancel()
            if rid == run_id:
                self._running.pop(attempt_id, None)

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    async def schedule(self) -> None:
        """扫描全部 queued 尝试，在三级限额内启动（串行化防并发重复启动）。"""
        async with self._schedule_lock:
            async with db_session() as session:
                settings = await self.settings.get(session)
                stmt = (
                    select(Attempt, Task)
                    .join(Task, Attempt.task_id == Task.id)
                    .where(Attempt.status == AttemptStatus.queued)
                    .order_by(Task.priority, Attempt.created_at)
                )
                rows = (await session.execute(stmt)).all()
                global_limit = int(settings["global_parallel_limit"])
                run_limit = int(settings["run_parallel_limit"])

                agent_rows = (
                    await session.execute(
                        select(Attempt.agent_id, func.count())
                        .where(Attempt.status.in_([AttemptStatus.running, AttemptStatus.awaiting_approval]))
                        .group_by(Attempt.agent_id)
                    )
                ).all()
                running_per_agent: dict[str, int] = {a: c for a, c in agent_rows}

                running_total = sum(1 for t, _ in self._running.values() if not t.done())
                running_per_run: dict[str, int] = {}
                for _, (_, rid) in self._running.items():
                    running_per_run[rid] = running_per_run.get(rid, 0) + 1

                for attempt, task in rows:
                    if running_total >= global_limit:
                        break
                    if attempt.id in self._running:
                        continue  # 已在启动中/运行中
                    run = await session.get(Run, task.run_id)
                    if run is None or run.status != RunStatus.running:
                        continue
                    if running_per_run.get(task.run_id, 0) >= run_limit:
                        continue
                    agent = await session.get(Agent, attempt.agent_id)
                    if agent is None:
                        continue
                    if running_per_agent.get(agent.id, 0) >= agent.max_concurrency:
                        continue
                    self._running[attempt.id] = (
                        asyncio.create_task(self._run_attempt(attempt.id)),
                        task.run_id,
                    )
                    running_total += 1
                    running_per_run[task.run_id] = running_per_run.get(task.run_id, 0) + 1
                    running_per_agent[agent.id] = running_per_agent.get(agent.id, 0) + 1

    # ------------------------------------------------------------------
    # 尝试执行
    # ------------------------------------------------------------------
    async def _run_attempt(self, attempt_id: str) -> None:
        try:
            ctx = await self._materialize(attempt_id)
            if ctx is None:
                return
            executor = self.executors.get(ctx.executor)
            usage = Usage()
            async for event in executor.start(ctx):
                usage = await self._translate_event(ctx, event, usage)
        except asyncio.CancelledError:
            await self._mark_cancelled(attempt_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("尝试执行异常 attempt=%s", attempt_id)
            await self.handle_crashed_attempt(attempt_id, f"执行异常: {e}")
        finally:
            # 各结果处理器已按需唤醒主 agent（自动重试路径刻意不唤醒），
            # 这里只负责补位调度，不再发通用唤醒
            self._running.pop(attempt_id, None)
            await self._schedule_next()

    async def _schedule_next(self) -> None:
        try:
            await self.schedule()
        except Exception:
            logger.exception("重新调度失败")

    async def _model_config_for_agent(self, session: Any, agent: Agent) -> Any:
        from app.runtime.model_config import model_config_from_profile

        profile = await session.get(ModelProfile, agent.model_profile_id)
        if profile is None:
            result = await session.execute(
                select(ModelProfile)
                .where(ModelProfile.is_default_worker.is_(True))
                .limit(1)
            )
            profile = result.scalar_one_or_none()
        if profile is None:
            return None
        api_key = ""
        if profile.api_key_secret_id:
            from app import secrets as secrets_util

            secret = await session.get(Secret, profile.api_key_secret_id)
            if secret is not None:
                try:
                    api_key = secrets_util.decrypt(secret.encrypted_value)
                except ValueError:
                    api_key = ""
        return model_config_from_profile(profile, api_key)

    async def _materialize(self, attempt_id: str) -> RunContext | None:
        """物化运行上下文（§9.3）。"""
        async with db_session() as session:
            attempt = await session.get(Attempt, attempt_id)
            if attempt is None or attempt.status != AttemptStatus.queued:
                return None
            task = await session.get(Task, attempt.task_id)
            if task is None:
                return None
            run = await session.get(Run, task.run_id)
            agent = await session.get(Agent, attempt.agent_id)
            if run is None or agent is None:
                return None

            brief = TaskBrief(**(attempt.brief or {}))
            input_artifacts: list[tuple[Artifact, Path]] = []
            inputs_summary_parts: list[str] = []
            base = self.artifacts._base_dir()  # noqa: SLF001
            for ref in brief.inputs:
                art = await session.get(Artifact, ref.artifact_id)
                if art is not None:
                    input_artifacts.append((art, base / art.storage_path))
                    inputs_summary_parts.append(f"- {art.name}：{art.summary or ref.usage}")

            workspace = build_workspace(
                run_id=run.id,
                task_key=task.key,
                attempt_no=attempt.number,
                brief=brief,
                input_artifacts=input_artifacts,
            )

            budget = brief.budget or Budget(**(agent.default_budget or {}))
            model_cfg = await self._model_config_for_agent(session, agent)
            if model_cfg is None:
                await self.handle_crashed_attempt(attempt_id, "agent 的模型配置不可用")
                return None

            # --- 第二期：工具库物化（§8、§9.2）---
            agent_tools = await self.tools.build_agent_tools(session, agent)
            if agent.executor.value == "sdk":
                self.tools.materialize_claude_assets(Path(workspace), agent_tools)
            settings_snapshot = await self.settings.get(session)
            masker = SecretMasker(agent_tools.secret_values)

            system_prompt = assemble_system_prompt(
                agent.system_prompt, brief, "\n".join(inputs_summary_parts)
            )

            attempt.status = AttemptStatus("running")
            attempt.started_at = attempt.started_at or utcnow()
            attempt.workspace_path = str(workspace)
            await transition_task(
                session, self.events, task, TaskStatus.running, reason="开始执行", actor="system"
            )
            tool_names = ["submit_report"] + [d.name for d in agent_tools.defs]
            if agent.executor.value == "sdk" and agent_tools.mcp_specs:
                tool_names += [f"mcp__{slug}__*" for slug in agent_tools.mcp_specs]
            await self.events.emit(
                session,
                type="attempt.started",
                actor=f"agent:{agent.slug}",
                run_id=run.id,
                task_key=task.key,
                attempt_id=attempt.id,
                payload={
                    "task_key": task.key,
                    "number": attempt.number,
                    "agent_slug": agent.slug,
                    "workspace_path": str(workspace),
                    "tools": tool_names,
                    "model": model_cfg.model,
                    "budget": budget.model_dump(mode="json"),
                },
            )

            gate = self._pause_events.setdefault(run.id, asyncio.Event())
            gate.set()
            ctx = RunContext(
                run_id=run.id,
                task_key=task.key,
                attempt_id=attempt.id,
                attempt_no=attempt.number,
                agent_slug=agent.slug,
                workspace=str(workspace),
                system_prompt=system_prompt,
                first_input=first_user_input(brief),
                budget=budget,
                model=model_cfg,
                executor=agent.executor.value,
                pause_event=gate,
                agent_id=agent.id,
                tool_defs=agent_tools.defs,
                mcp_specs=agent_tools.mcp_specs,
                masker=masker,
                tool_gate=self._tool_gate,
                allow_custom_python=bool(settings_snapshot.get("allow_custom_python_tools")),
            )
            return ctx

    # ------------------------------------------------------------------
    # 工具门（§8.9 权限链 + §11 审批）
    # ------------------------------------------------------------------
    async def _tool_gate(
        self, ctx: RunContext, tool_name: str, arguments: dict[str, Any]
    ) -> GateResult:
        from app.tools.permission import parse_tool_name

        slug, _sub = parse_tool_name(tool_name)
        # 第一段：权限链（短会话，避免与审批门嵌套）
        async with db_session() as session:
            settings = await self.settings.get(session)
            decision = await check_tool_call(
                session,
                settings,
                agent_id=ctx.agent_id,
                tool_name=tool_name,
                arguments=arguments,
                remembered=self.approvals.remembers(ctx.run_id, ctx.agent_id, slug),
            )
            if decision.verdict.value != "ask":
                if decision.allowed:
                    return GateResult(approved=True, basis=decision.basis)
                # deny：记录 tool.call.blocked（§12.4）
                await self.events.emit(
                    session,
                    type="tool.call.blocked",
                    actor=f"agent:{ctx.agent_slug}",
                    run_id=ctx.run_id,
                    task_key=ctx.task_key,
                    attempt_id=ctx.attempt_id,
                    payload={
                        "task_key": ctx.task_key,
                        "attempt_id": ctx.attempt_id,
                        "tool": tool_name,
                        "reason": decision.basis,
                    },
                )
                return GateResult(approved=False, basis=decision.basis)

        # 第二段：ask → 审批门（门内部自管会话与等待）
        masker = ctx.masker if isinstance(ctx.masker, SecretMasker) else None
        masked_args = masker.mask_data(arguments) if masker else arguments
        action = tool_name
        for key in ("command", "url", "path", "query", "text"):
            if isinstance(arguments.get(key), str):
                action = f"{tool_name}: {arguments[key][:120]}"
                break
        approved, note = await self.approvals.request(
            ctx,
            tool_slug=decision.tool_slug,
            tool_name=tool_name,
            action=action,
            arguments_masked=dict(masked_args) if isinstance(masked_args, dict) else {},
            reason=decision.basis,
            risk_note="该工具配置为需审批（ask）",
        )
        if approved:
            return GateResult(approved=True, basis=f"审批通过：{note}")
        async with db_session() as session:
            await self.events.emit(
                session,
                type="tool.call.blocked",
                actor=f"agent:{ctx.agent_slug}",
                run_id=ctx.run_id,
                task_key=ctx.task_key,
                attempt_id=ctx.attempt_id,
                payload={
                    "task_key": ctx.task_key,
                    "attempt_id": ctx.attempt_id,
                    "tool": tool_name,
                    "reason": f"审批拒绝：{note or '用户拒绝'}",
                },
            )
        return GateResult(approved=False, basis=f"审批拒绝：{note or '用户拒绝'}")

    # ------------------------------------------------------------------
    # 事件翻译（§9.7）
    # ------------------------------------------------------------------
    async def _translate_event(self, ctx: RunContext, event: Any, usage: Usage) -> Usage:
        actor = f"agent:{ctx.agent_slug}"
        if isinstance(event, EvOutputDelta):
            await self.events.emit(
                None,  # type: ignore[arg-type]  # 仅推送事件不需要会话
                type="attempt.output.delta",
                actor=actor,
                run_id=ctx.run_id,
                task_key=ctx.task_key,
                attempt_id=ctx.attempt_id,
                payload={"text": event.text},
                persist=False,
            )
        elif isinstance(event, (EvOutputTurn, EvThinking)):
            etype = "attempt.output" if isinstance(event, EvOutputTurn) else "attempt.thinking"
            async with db_session() as session:
                await self.events.emit(
                    session, type=etype, actor=actor, run_id=ctx.run_id,
                    task_key=ctx.task_key, attempt_id=ctx.attempt_id,
                    payload={"text": event.text},
                )
        elif isinstance(event, EvUsage):
            usage = event.usage
            async with db_session() as session:
                attempt = await session.get(Attempt, ctx.attempt_id)
                if attempt is not None:
                    attempt.usage = usage.model_dump(mode="json")
                await self.events.emit(
                    session, type="attempt.usage", actor=actor, run_id=ctx.run_id,
                    task_key=ctx.task_key, attempt_id=ctx.attempt_id,
                    payload={"usage": usage.model_dump(mode="json")},
                )
            if self.engine is not None:
                await self.engine.accumulate_usage(ctx.run_id, usage)
        elif isinstance(event, EvToolStarted):
            async with db_session() as session:
                masker = ctx.masker if isinstance(ctx.masker, SecretMasker) else None
                shown_args = masker.mask_data(event.arguments) if masker else event.arguments
                await self.events.emit(
                    session, type="tool.call.started", actor=actor, run_id=ctx.run_id,
                    task_key=ctx.task_key, attempt_id=ctx.attempt_id,
                    payload={
                        "task_key": ctx.task_key, "attempt_id": ctx.attempt_id,
                        "tool": event.tool, "arguments": shown_args,
                        "permission": "allow", "permission_basis": "平台内置工具",
                    },
                )
        elif isinstance(event, EvToolFinished):
            async with db_session() as session:
                masker = ctx.masker if isinstance(ctx.masker, SecretMasker) else None
                summary = (
                    masker.mask_text(event.result_summary)
                    if masker
                    else event.result_summary
                )
                await self.events.emit(
                    session, type="tool.call.finished", actor=actor, run_id=ctx.run_id,
                    task_key=ctx.task_key, attempt_id=ctx.attempt_id,
                    payload={
                        "task_key": ctx.task_key, "attempt_id": ctx.attempt_id,
                        "tool": event.tool, "ok": event.ok,
                        "duration_ms": event.duration_ms,
                        "result_summary": summary,
                    },
                )
        elif isinstance(event, EvReported):
            await self._handle_reported(ctx, event.report)
        elif isinstance(event, (EvCrashed, EvTimedOut)):
            if isinstance(event, EvCrashed):
                await self.handle_crashed_attempt(ctx.attempt_id, event.error, event.last_output)
            else:
                async with db_session() as session:
                    attempt = await session.get(Attempt, ctx.attempt_id)
                    if attempt is not None:
                        attempt.status = AttemptStatus("timed_out")
                        attempt.error = (event.error or "执行超时")[:2000]
                        attempt.ended_at = utcnow()
                await self.handle_crashed_attempt(ctx.attempt_id, event.error or "执行超时")
        return usage

    # ------------------------------------------------------------------
    # 回报处理
    # ------------------------------------------------------------------
    async def _handle_reported(self, ctx: RunContext, report_payload: dict[str, Any]) -> None:
        usage_data = report_payload.pop("_usage", {})
        report_payload.pop("_last_output", None)
        auto_retry = False
        retry_reason = ""

        async with db_session() as session:
            attempt = await session.get(Attempt, ctx.attempt_id)
            if attempt is None:
                return
            task = await session.get(Task, attempt.task_id)
            if task is None:
                return
            run = await session.get(Run, task.run_id)
            if run is None:
                return
            agent = await session.get(Agent, attempt.agent_id)

            for d in report_payload.get("deliverables") or []:
                content = d.get("content")
                name = d.get("name") or Path(d.get("path") or "deliverable.md").name
                actor = f"agent:{agent.slug}" if agent else "system"
                if content is not None:
                    await self.artifacts.save_bytes(
                        session,
                        run_id=run.id,
                        data=str(content).encode("utf-8"),
                        name=name,
                        task_id=task.id,
                        task_key=task.key,
                        attempt_id=attempt.id,
                        attempt_no=attempt.number,
                        kind="markdown" if str(name).endswith(".md") else "file",
                        summary=str(d.get("summary") or "")[:500],
                        actor=actor,
                    )
                elif d.get("path"):
                    ws_file = Path(ctx.workspace) / str(d["path"])
                    if ws_file.exists():
                        await self.artifacts.save_bytes(
                            session,
                            run_id=run.id,
                            data=ws_file.read_bytes(),
                            name=ws_file.name,
                            task_id=task.id,
                            task_key=task.key,
                            attempt_id=attempt.id,
                            attempt_no=attempt.number,
                            summary=str(d.get("summary") or "")[:500],
                            actor=actor,
                        )

            attempt.report = report_payload
            attempt.status = AttemptStatus("reported")
            attempt.ended_at = utcnow()
            if usage_data:
                attempt.usage = usage_data

            await self.events.emit(
                session,
                type="attempt.reported",
                actor=f"agent:{agent.slug}" if agent else "system",
                run_id=run.id,
                task_key=task.key,
                attempt_id=attempt.id,
                payload={
                    "task_key": task.key,
                    "number": attempt.number,
                    "report": report_payload,
                },
            )

            status = report_payload.get("status")
            blocker_type = (report_payload.get("blocker") or {}).get("type")

            if status in ("done", "partial"):
                await transition_task(
                    session, self.events, task, TaskStatus.reported,
                    reason="已提交回报", actor="system",
                )
            else:
                if blocker_type == BlockerType.external_error.value:
                    if await self._auto_retry_count(session, task) < 2:
                        auto_retry = True
                        retry_reason = "external_error 自动重试"
                await transition_task(
                    session, self.events, task, TaskStatus.blocked,
                    reason=f"回报 {status}（{blocker_type or '无 blocker'}）",
                    actor="system",
                )

        if auto_retry:
            await self._schedule_auto_retry(task_id=task.id, source_attempt_id=ctx.attempt_id,
                                            reason=retry_reason, delay=_external_error_retry_delay())
        elif self.engine is not None:
            await self.engine.wake(ctx.run_id, f"attempt_reported:{ctx.task_key}")
        await self._schedule_next()

    # ------------------------------------------------------------------
    # 崩溃与自动重试
    # ------------------------------------------------------------------
    async def handle_crashed_attempt(
        self, attempt_id: str, error: str, last_output: str = ""
    ) -> None:
        auto_retry = False
        run_id: str | None = None
        async with db_session() as session:
            attempt = await session.get(Attempt, attempt_id)
            if attempt is None:
                return
            if attempt.status in (AttemptStatus.reported, AttemptStatus.crashed, AttemptStatus.timed_out, AttemptStatus.cancelled):
                return
            task = await session.get(Task, attempt.task_id)
            if task is None:
                return
            run = await session.get(Run, task.run_id)
            run_id = run.id if run else None
            attempt.status = AttemptStatus("crashed")
            attempt.error = error[:2000]
            attempt.ended_at = utcnow()
            await self.events.emit(
                session,
                type="attempt.crashed",
                actor="system",
                run_id=run_id,
                task_key=task.key,
                attempt_id=attempt.id,
                payload={
                    "task_key": task.key,
                    "number": attempt.number,
                    "error": error,
                    "last_output": last_output[-500:],
                },
            )
            if task.status in (TaskStatus.running, TaskStatus.awaiting_approval):
                if await self._auto_retry_count(session, task) == 0:
                    auto_retry = True
                await transition_task(
                    session, self.events, task, TaskStatus.blocked,
                    reason=f"尝试崩溃：{error[:100]}", actor="system",
                )

        if auto_retry:
            await self._schedule_auto_retry(
                task_id=task.id, source_attempt_id=attempt_id, reason=f"崩溃自动重试: {error[:80]}"
            )
        elif self.engine is not None and run_id is not None:
            await self.engine.wake(run_id, f"attempt_crashed:{task.key}")
        await self._schedule_next()

    async def _auto_retry_count(self, session: Any, task: Task) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(Attempt)
            .where(Attempt.task_id == task.id, Attempt.auto_retry_of.is_not(None))
        )
        return int(result.scalar_one_or_none() or 0)

    async def _schedule_auto_retry(
        self, *, task_id: str, source_attempt_id: str, reason: str, delay: float = 0.0
    ) -> None:
        async def _later() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                async with db_session() as session:
                    source = await session.get(Attempt, source_attempt_id)
                    if source is None:
                        return
                    task = await session.get(Task, task_id)
                    if task is None or task.status.is_terminal:
                        return
                    agent = await session.get(Agent, source.agent_id)
                    if agent is None or agent.status != AgentStatus.active:
                        if self.engine:
                            await self.engine.wake(task.run_id, "auto_retry_failed")
                        return
                    max_no = (
                        await session.execute(
                            select(func.max(Attempt.number)).where(Attempt.task_id == task.id)
                        )
                    ).scalar_one_or_none() or 0
                    retry = Attempt(
                        id=new_id("att"),
                        task_id=task.id,
                        number=max_no + 1,
                        agent_id=agent.id,
                        agent_version=agent.version,
                        brief=source.brief,
                        status="queued",
                        counted=False,
                        auto_retry_of=source.id,
                    )
                    session.add(retry)
                    await session.flush()
                    if task.status == TaskStatus.blocked:
                        await transition_task(
                            session, self.events, task, TaskStatus.queued,
                            reason=reason, actor="system",
                        )
                    await record_decision(
                        session,
                        self.events,
                        run_id=task.run_id,
                        type="retried",
                        task_id=task.id,
                        task_key=task.key,
                        summary=f"{task.key} 自动重试（{reason}）",
                        rationale=reason,
                        made_by="rule",
                    )
                await self.schedule()
            finally:
                self._retry_timers.pop(task_id, None)

        self._retry_timers[task_id] = asyncio.create_task(_later())

    async def _mark_cancelled(self, attempt_id: str) -> None:
        async with db_session() as session:
            attempt = await session.get(Attempt, attempt_id)
            if attempt is None:
                return
            task = await session.get(Task, attempt.task_id)
            if task is None:
                return
            run = await session.get(Run, task.run_id)
            if attempt.status not in (AttemptStatus.reported, AttemptStatus.crashed, AttemptStatus.timed_out, AttemptStatus.cancelled):
                attempt.status = AttemptStatus("cancelled")
                attempt.ended_at = utcnow()
                await self.events.emit(
                    session,
                    type="attempt.cancelled",
                    actor="user",
                    run_id=run.id if run else None,
                    task_key=task.key,
                    attempt_id=attempt.id,
                    payload={
                        "task_key": task.key,
                        "number": attempt.number,
                        "reason": "运行被取消",
                    },
                )
                if task.status == TaskStatus.running:
                    await transition_task(
                        session, self.events, task, TaskStatus.cancelled,
                        reason="运行取消", actor="user",
                    )

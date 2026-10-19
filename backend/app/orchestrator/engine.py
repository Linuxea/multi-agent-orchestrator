"""协调引擎（§5）：主 agent 循环、计划管理、控制工具、协调规则、复核。

模型负责判断，引擎负责执行规则和守住边界；任何越界调用被拒绝并把原因返回给模型。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.enums import (
    AgentStatus,
    ArtifactStatus,
    BlockerType,
    DecisionType,
    PlanApprovalMode,
    ReportStatus,
    RunOutcome,
    RunStatus,
    TaskStatus,
)
from app.events.service import EventService
from app.ids import new_id
from app.models import (
    Agent,
    Artifact,
    Attempt,
    Decision,
    Message,
    Plan,
    Playbook,
    Run,
    Task,
)
from app.orchestrator.context import (
    COORDINATION_RULES_MD,
    build_run_digest,
    playbook_stage_keys,
    render_orchestrator_system,
    session_team,
)
from app.orchestrator.helpers import (
    db_session,
    record_decision,
    transition_run,
    transition_task,
    valid_task_key,
)
from app.orchestrator.states import InvalidTransition
from app.orchestrator.tools import control_tool_schemas
from app.registry.service import RegistryService
from app.runtime.model_client import ModelCaller, ModelError
from app.runtime.types import ModelConfig
from app.schemas.common import Budget, Usage
from app.schemas.contracts import TaskBrief
from app.schemas.plan import PlanTaskSpec, Review
from app.settings import SettingsService
from app.utils import text_similarity

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS_PER_WAKE = 15  # §5.11
MAX_REWORK_SIMILARITY = 0.9  # §11.3 死循环防护


class ControlToolError(Exception):
    """控制工具被引擎拒绝（原因会返回给模型并记录 orchestrator.tool_rejected）。"""


@dataclass
class WakeState:
    """一次唤醒内的跟踪状态。"""

    run_id: str
    read_artifact_ids: set[str] = field(default_factory=set)
    tool_calls: int = 0


@dataclass
class PendingQuestion:
    question_id: str
    question: str
    options: list[str]
    blocking_task_keys: list[str]


class CoordinationEngine:
    def __init__(
        self,
        events: EventService,
        registry: RegistryService,
        settings: SettingsService,
    ) -> None:
        self.events = events
        self.registry = registry
        self.settings = settings
        self.scheduler: Any = None  # 注入以避免循环依赖

        self._wake_queues: dict[str, deque[str]] = {}
        self._wake_tasks: dict[str, asyncio.Task[None]] = {}
        self._active: set[str] = set()
        self._threads: dict[str, list[dict[str, Any]]] = {}
        self._questions: dict[str, dict[str, PendingQuestion]] = {}
        self._budget_warned: set[str] = set()
        self._budget_exceeded: set[str] = set()

    # ------------------------------------------------------------------
    # 入口：用户消息与唤醒
    # ------------------------------------------------------------------
    async def submit_user_message(
        self, session_id: str, content: str
    ) -> tuple[Message, Run]:
        """发送消息：有进行中的运行则作为补充说明，否则创建新运行（§12.2）。"""
        async with db_session() as session:
            result = await session.execute(
                select(Run)
                .where(Run.session_id == session_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            latest_run = result.scalar_one_or_none()
            active_run = (
                latest_run
                if latest_run is not None and not latest_run.status.is_terminal
                else None
            )

            message = Message(
                id=new_id("msg"), session_id=session_id, role="user", content=content
            )
            session.add(message)
            await session.flush()

            if active_run is not None:
                message.run_id = active_run.id
                run = active_run
                await self.events.emit(
                    session,
                    type="user.intervened",
                    actor="user",
                    run_id=run.id,
                    payload={"action": "supplement", "details": {"message_id": message.id}},
                )
            else:
                run = Run(
                    id=new_id("run"),
                    session_id=session_id,
                    trigger_message_id=message.id,
                    status=RunStatus.created,
                )
                message.run_id = run.id
                session.add(run)
                await session.flush()
                settings = await self.settings.get(session)
                run.budget = dict(settings.get("default_run_budget") or {})
                await self.events.emit(
                    session,
                    type="run.created",
                    actor="user",
                    run_id=run.id,
                    payload={
                        "session_id": session_id,
                        "trigger_message_id": message.id,
                        "user_request": content,
                    },
                )
        await self.wake(run.id, "new_user_message")
        return message, run

    async def wake(self, run_id: str, reason: str) -> None:
        """事件驱动唤醒；2 秒内的多个触发合并为一次（§5.11）。"""
        q = self._wake_queues.setdefault(run_id, deque())
        q.append(reason)
        if run_id not in self._wake_tasks or self._wake_tasks[run_id].done():
            self._wake_tasks[run_id] = asyncio.create_task(self._process_wakes(run_id))

    async def _process_wakes(self, run_id: str) -> None:
        merge = get_config().wake_merge_seconds
        while self._wake_queues.get(run_id):
            await asyncio.sleep(merge)
            reasons = list(self._wake_queues.get(run_id, deque()))
            self._wake_queues[run_id].clear()
            if not reasons:
                continue
            if run_id in self._active:
                # 正在编排：稍后再取
                self._wake_queues[run_id].extend(reasons)
                await asyncio.sleep(merge)
                continue
            try:
                await self._orchestrate_once(run_id, reasons)
            except Exception:
                logger.exception("编排失败 run=%s", run_id)
                await self._fail_run(run_id, "编排过程发生内部错误")
        self._wake_tasks.pop(run_id, None)

    def is_busy(self, run_id: str) -> bool:
        return bool(self._wake_queues.get(run_id)) or run_id in self._active

    async def quiesce(self, run_id: str, timeout: float = 60.0) -> bool:  # noqa: ASYNC109
        """等待运行静止（无待处理唤醒、无编排、无运行中尝试）。测试与 CLI 使用。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (
                not self.is_busy(run_id)
                and self.scheduler is not None
                and not self.scheduler.has_running(run_id)
                and not self.scheduler.has_pending_retry(run_id)
            ):
                return True
            await asyncio.sleep(0.01)
        return False

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    # 总是唤醒的语义原因（§5.11：用户消息 / 回答 / 预算 / 恢复等）
    ALWAYS_WAKE_REASONS = {
        "new_user_message",
        "user_answered",
        "plan_rejected",
        "engine_recovered",
        "budget_extended",
        "budget_exceeded",
        "resumed",
        "task_cancelled_by_user",
        "task_reassigned_by_user",
        "task_accepted_by_user",
        "task_redispatched_by_user",
    }

    async def _should_orchestrate(
        self, session: AsyncSession, run: Run, reasons: list[str]
    ) -> bool:
        """跳过无实际可推进事项的唤醒，避免空转与无效模型调用。"""
        if any(r in self.ALWAYS_WAKE_REASONS or r.startswith("task_") for r in reasons):
            return True
        if run.status in (
            RunStatus.awaiting_plan_approval,
            RunStatus.clarifying,
            RunStatus.awaiting_user,
        ):
            return False
        if run.status == RunStatus.synthesizing:
            return True
        if run.status == RunStatus.created:
            return True
        if run.status == RunStatus.planning and run.current_plan_version == 0:
            return True
        tasks = await self._run_tasks(session, run.id)
        if not tasks:
            return run.status == RunStatus.planning
        actionable = {
            TaskStatus.ready,
            TaskStatus.reported,
            TaskStatus.blocked,
            TaskStatus.needs_rework,
        }
        for t in tasks:
            if t.status == TaskStatus.blocked and await self._pending_auto_retry(session, t):
                continue  # 调度器将自动重试，暂不需主 agent 介入（防陈旧唤醒竞态）
            if t.status in actionable:
                return True
        return False

    async def _pending_auto_retry(self, session: AsyncSession, task: Task) -> bool:
        """该 blocked 任务的最新尝试是否处于"调度器将自动重试"的窗口（§5.7）。"""
        result = await session.execute(
            select(Attempt)
            .where(Attempt.task_id == task.id)
            .order_by(Attempt.number.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        if latest is None:
            return False
        retried = await self.scheduler._auto_retry_count(session, task) if self.scheduler else 0
        report = latest.report or {}
        blocker = report.get("blocker") or {}
        if latest.status.value == "reported" and blocker.get("type") == "external_error":
            return retried < 2
        if latest.status.value in ("crashed", "timed_out"):
            return retried == 0
        return False

    async def _orchestrate_once(self, run_id: str, reasons: list[str]) -> None:
        self._active.add(run_id)
        try:
            async with db_session() as session:
                run = await self._load_run(session, run_id)
                if run is None or run.status.is_terminal:
                    return
                if run.status == RunStatus.paused:
                    return
                if not await self._should_orchestrate(session, run, reasons):
                    return
                await self.events.emit(
                    session,
                    type="orchestrator.woke",
                    actor="orchestrator",
                    run_id=run_id,
                    payload={"reasons": reasons},
                )
                if run.status == RunStatus.created:
                    await transition_run(
                        session, self.events, run, RunStatus.planning, reason="开始编排"
                    )
                await self._check_synthesizing(session, run)

                model_cfg = await self._orchestrator_model_config(session)
                if model_cfg is None:
                    await self._fail_run_in_session(session, run, "未配置主 agent 模型")
                    return

            ws = WakeState(run_id=run_id)
            await self._run_model_loop(run_id, reasons, ws)
        finally:
            self._active.discard(run_id)

    async def _run_model_loop(self, run_id: str, reasons: list[str], ws: WakeState) -> None:
        caller = ModelCaller(await self._model_cfg_for_run(run_id))  # type: ignore[arg-type]
        thread = self._threads.setdefault(run_id, [])

        async with db_session() as session:
            run = await self._load_run(session, run_id)
            assert run is not None
            digest = await self._build_digest(session, run)
            system = render_orchestrator_system(digest, reasons, COORDINATION_RULES_MD)
            agent_slugs = [
                a.slug
                for a in await self.registry.list_agents(session, [AgentStatus.active])
            ]
            tools = control_tool_schemas(agent_slugs)
            wake_msg = (
                "【系统唤醒】触发原因："
                + "、".join(reasons)
                + "\n\n当前运行状态摘要：\n"
                + digest
            )
            thread.append({"role": "user", "content": wake_msg})
            self._compact_thread(thread, caller.cfg.context_window)

        try:
            while True:
                try:
                    response = await asyncio.wait_for(
                        caller.create(system=system, messages=thread, tools=tools),
                        timeout=get_config().model_call_deadline_seconds,
                    )
                except TimeoutError as e:
                    raise ModelError(
                        f"模型调用超过墙钟 {get_config().model_call_deadline_seconds:.0f}s"
                        "（网络挂起看门狗，请重试或稍后再试）"
                    ) from e
                await self._track_orchestrator_usage(run_id, response.usage, caller.cfg)

                for block in response.blocks:
                    if block.type == "thinking" and block.text:
                        async with db_session() as session:
                            await self.events.emit(
                                session,
                                type="orchestrator.thinking",
                                actor="orchestrator",
                                run_id=run_id,
                                payload={"text": block.text},
                            )

                tool_blocks = [b for b in response.blocks if b.type == "tool_use"]
                if not tool_blocks:
                    text = "".join(b.text for b in response.blocks if b.type == "text")
                    thread.append({"role": "assistant", "content": text or "（无输出）"})
                    if text:
                        async with db_session() as session:
                            await self.events.emit(
                                session,
                                type="orchestrator.message",
                                actor="orchestrator",
                                run_id=run_id,
                                payload={"text": text},
                            )
                    break

                thread.append(
                    {"role": "assistant", "content": assistant_content(response.blocks, tool_blocks)}
                )
                results: list[dict[str, Any]] = []
                for block in tool_blocks:
                    ws.tool_calls += 1
                    if ws.tool_calls > MAX_TOOL_CALLS_PER_WAKE:
                        result = {
                            "error": f"本次唤醒工具调用超过 {MAX_TOOL_CALLS_PER_WAKE} 次上限"
                        }
                        await self._emit_rejected(run_id, "__limit__", block.tool_input, result["error"])
                    else:
                        result = await self._execute_control_tool(ws, block.tool_name, block.tool_input)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_id,
                            "content": _json_dumps(result),
                        }
                    )
                thread.append({"role": "user", "content": results})
        except ModelError as e:
            await self._fail_run(run_id, f"主 agent 模型调用失败: {e}")

        # 收尾检查：是否全部终态 / 是否需要继续
        await self._post_orchestrate(run_id)

    async def _post_orchestrate(self, run_id: str) -> None:
        async with db_session() as session:
            run = await self._load_run(session, run_id)
            if run is None or run.status.is_terminal:
                return
            tasks = await self._run_tasks(session, run_id)
            if run.status == RunStatus.running and tasks and all(
                t.status.is_terminal for t in tasks
            ):
                await transition_run(
                    session, self.events, run, RunStatus.synthesizing, reason="全部任务终态"
                )
                await self.wake(run_id, "all_tasks_terminal")

    # ------------------------------------------------------------------
    # 控制工具分发
    # ------------------------------------------------------------------
    async def _execute_control_tool(
        self, ws: WakeState, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        rationale = str(arguments.get("rationale") or "").strip()
        if not rationale:
            await self._emit_rejected(
                ws.run_id, name, arguments, "缺少 rationale：每次工具调用必须附理由"
            )
            return {"error": "缺少 rationale：每次工具调用必须附理由（≤300 字）"}
        if len(rationale) > 300:
            await self._emit_rejected(ws.run_id, name, arguments, "rationale 超过 300 字")
            return {"error": "rationale 超过 300 字上限"}

        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            await self._emit_rejected(ws.run_id, name, arguments, f"未知工具 {name}")
            return {"error": f"未知工具 {name}"}
        try:
            result: dict[str, Any] = dict(await handler(ws, arguments))
            async with db_session() as session:
                await self.events.emit(
                    session,
                    type="orchestrator.tool_called",
                    actor="orchestrator",
                    run_id=ws.run_id,
                    payload={
                        "tool": name,
                        "arguments": _safe_args(arguments),
                        "rationale": rationale,
                        "result_summary": _json_dumps(result)[:400],
                    },
                )
            return result
        except ControlToolError as e:
            await self._emit_rejected(ws.run_id, name, arguments, str(e))
            return {"error": str(e)}
        except InvalidTransition as e:
            await self._emit_rejected(ws.run_id, name, arguments, str(e))
            return {"error": str(e)}
        except ValidationError as e:
            msg = f"参数不合法: {e.errors()[:3]}"
            await self._emit_rejected(ws.run_id, name, arguments, msg)
            return {"error": msg}

    async def _emit_rejected(
        self, run_id: str, tool: str, arguments: dict[str, Any], reason: str
    ) -> None:
        async with db_session() as session:
            await self.events.emit(
                session,
                type="orchestrator.tool_rejected",
                actor="orchestrator",
                run_id=run_id,
                payload={"tool": tool, "arguments": _safe_args(arguments), "reason": reason},
            )

    # --- 只读工具 ---
    async def _tool_search_agents(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        async with db_session() as session:
            team_slug = str(args.get("team") or "").strip()
            if team_slug:
                team = await self.registry.find_team(session, team_slug)
                if team is None:
                    raise ControlToolError(f"团队不存在: {team_slug}")
                agents = await self.registry.list_agents(session, [AgentStatus.active])
                agents = self.registry.resolve_team_members(team, agents)
            else:
                agents = await self.registry.search_agents(
                    session,
                    query=str(args.get("query") or ""),
                    tags=args.get("tags") or None,
                )
            return {
                "team": team_slug or None,
                "agents": [
                    {
                        "slug": a.slug,
                        "name": a.name,
                        "description": a.description,
                        "capability_tags": a.capability_tags,
                        "executor": a.executor.value,
                    }
                    for a in agents
                ],
            }

    async def _tool_get_agent(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        async with db_session() as session:
            agent = await self.registry.get_agent_by_slug(session, str(args.get("agent_slug")))
            if agent is None or agent.status != AgentStatus.active:
                raise ControlToolError(f"agent 不存在或不可用: {args.get('agent_slug')}")
            return {
                "slug": agent.slug,
                "name": agent.name,
                "description": agent.description,
                "role_detail": agent.role_detail,
                "capability_tags": agent.capability_tags,
                "executor": agent.executor.value,
                "input_contract": agent.input_contract,
                "output_contract": agent.output_contract,
                "max_concurrency": agent.max_concurrency,
            }

    async def _tool_list_playbooks(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        async with db_session() as session:
            team_slug = str(args.get("team") or "").strip()
            if team_slug:
                team = await self.registry.find_team(session, team_slug)
                if team is None:
                    raise ControlToolError(f"团队不存在: {team_slug}")
                pb = (
                    await session.get(Playbook, team.playbook_id)
                    if team.playbook_id
                    else None
                )
                playbooks = [pb] if pb is not None else []
            else:
                playbooks = await self.registry.list_playbooks(session)
            return {
                "playbooks": [
                    {"id": p.id, "name": p.name, "trigger_hint": p.trigger_hint, "stages": p.stages}
                    for p in playbooks
                ]
            }

    async def _tool_get_playbook(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        async with db_session() as session:
            pb = await self.registry.get_playbook(session, str(args.get("playbook_id")))
            if pb is None:
                raise ControlToolError("团队模板不存在")
            return {"id": pb.id, "name": pb.name, "trigger_hint": pb.trigger_hint, "stages": pb.stages}

    # --- 计划 ---
    async def _tool_propose_plan(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        tasks_spec = args.get("tasks") or []
        if not tasks_spec:
            raise ControlToolError("计划至少包含一个任务")
        specs = [PlanTaskSpec(**t) for t in tasks_spec]

        async with db_session() as session:
            run = await self._require_run(session, ws.run_id)
            settings = await self.settings.get(session)
            if len(specs) > int(settings["max_tasks_per_plan"]):
                raise ControlToolError(f"任务数超过上限 {settings['max_tasks_per_plan']}")

            keys = [s.key for s in specs]
            if len(set(keys)) != len(keys):
                raise ControlToolError("任务 key 重复")
            for s in specs:
                if not valid_task_key(s.key):
                    raise ControlToolError(f"非法任务 key: {s.key}")
                agent = await self.registry.get_agent_by_slug(session, s.agent_slug)
                if agent is None or agent.status != AgentStatus.active:
                    raise ControlToolError(f"任务 {s.key} 指派了不可用 agent: {s.agent_slug}")
            _check_acyclic(specs)

            # 编队剧本：阶段引用校验 + 偏离计算（D-4.5）
            bound = await session_team(session, run.session_id)
            stage_keys = playbook_stage_keys(bound[1]) if bound else []
            if stage_keys:
                for s in specs:
                    if s.playbook_stage_key and s.playbook_stage_key not in stage_keys:
                        raise ControlToolError(
                            f"任务 {s.key} 引用了剧本中不存在的阶段: {s.playbook_stage_key}"
                            f"（可用阶段: {', '.join(stage_keys)}）"
                        )
            elif any(s.playbook_stage_key for s in specs):
                raise ControlToolError("本会话未绑定团队剧本，不能填写 playbook_stage_key")
            deviates = (
                [k for k in stage_keys if k not in {s.playbook_stage_key for s in specs}]
                if stage_keys
                else []
            )

            version = run.current_plan_version + 1
            plan = Plan(
                id=new_id("plan"),
                run_id=run.id,
                version=version,
                tasks_snapshot=[s.model_dump() for s in specs],
                change_reason=str(args.get("rationale") or ""),
                proposed_by="orchestrator",
            )
            session.add(plan)
            await session.flush()

            for s in specs:
                agent = await self.registry.get_agent_by_slug(session, s.agent_slug)
                assert agent is not None
                task = Task(
                    id=new_id("task"),
                    run_id=run.id,
                    key=s.key,
                    title=s.title,
                    goal=s.goal,
                    assigned_agent_id=agent.id,
                    depends_on=list(s.depends_on),
                    status=TaskStatus.pending,
                    priority=s.priority,
                    optional=s.optional,
                    playbook_stage_key=s.playbook_stage_key,
                )
                session.add(task)
                await session.flush()
                await self.events.emit(
                    session,
                    type="task.created",
                    actor="orchestrator",
                    run_id=run.id,
                    task_key=s.key,
                    payload={
                        "key": s.key,
                        "title": s.title,
                        "agent_slug": s.agent_slug,
                        "depends_on": list(s.depends_on),
                    },
                )

            run.current_plan_version = version
            estimated = await self._estimate_plan_cost(session, [s.model_dump() for s in specs])
            await self.events.emit(
                session,
                type="plan.proposed",
                actor="orchestrator",
                run_id=run.id,
                payload={
                    "version": version,
                    "task_count": len(specs),
                    "estimated_cost": estimated,
                    "rationale": str(args.get("rationale") or ""),
                    "deviates_from_playbook": deviates,
                },
            )
            await record_decision(
                session,
                self.events,
                run_id=run.id,
                type=DecisionType.plan_created,
                summary=f"提出计划 v{version}（{len(specs)} 个任务）",
                rationale=str(args.get("rationale") or ""),
            )

            mode = PlanApprovalMode(settings["plan_approval_mode"])
            threshold = float(settings.get("plan_auto_approve_cost_threshold") or 0.5)
            needs_approval = (
                mode == PlanApprovalMode.always
                or (mode == PlanApprovalMode.auto_if_under and estimated >= threshold)
            )
            if needs_approval:
                await transition_run(
                    session,
                    self.events,
                    run,
                    RunStatus.awaiting_plan_approval,
                    reason=f"计划 v{version} 等待确认（预估费用 {estimated:.2f}）",
                    actor="orchestrator",
                )
                return {
                    "ok": True,
                    "plan_version": version,
                    "awaiting_approval": True,
                    "estimated_cost": estimated,
                    "note": "计划已提交，等待用户确认后任务才会进入 ready",
                }

            await self._approve_plan_internal(session, run, version, by="auto")
            ready = await self._recompute_readiness(session, run)
        if ready:
            await self.wake(ws.run_id, "tasks_ready")
        await self._kick_scheduler()
        return {"ok": True, "plan_version": version, "awaiting_approval": False, "ready_tasks": ready}

    async def _tool_revise_plan(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        changes = args.get("changes") or {}
        reason = str(args.get("reason") or args.get("rationale") or "")
        add_specs = [PlanTaskSpec(**t) for t in changes.get("add_tasks") or []]
        remove_keys: set[str] = set(changes.get("remove_task_keys") or [])
        update_specs: dict[str, dict[str, Any]] = {
            u.get("key"): u for u in changes.get("update_tasks") or [] if u.get("key")
        }

        async with db_session() as session:
            run = await self._require_run(session, ws.run_id)
            # 审批门槛：计划等待用户确认期间禁止修订——否则模型可用「修订刷新任务状态」
            # 绕过审批解锁任务并派发（真实事故：用户会话内说"确认"被模型误解，
            # 经 revise_plan 解锁任务后 dispatch，run 却停在待审批，调度器不动，形同卡死）
            if run.status == RunStatus.awaiting_plan_approval:
                raise ControlToolError(
                    "计划正在等待用户确认，不能修订；请等待用户批准或驳回后再调整"
                )
            tasks = {t.key: t for t in await self._run_tasks(session, ws.run_id)}
            settings = await self.settings.get(session)

            for key in remove_keys:
                t = tasks.get(key)
                if t is None:
                    raise ControlToolError(f"任务不存在: {key}")
                if t.status == TaskStatus.accepted:
                    raise ControlToolError(f"已 accepted 的任务不可删除: {key}")
            for key, updates in update_specs.items():
                t = tasks.get(key)
                if t is None:
                    raise ControlToolError(f"任务不存在: {key}")
                if t.status == TaskStatus.accepted:
                    raise ControlToolError(f"已 accepted 的任务不可修改: {key}")
                if "key" in updates and updates["key"] != key:
                    raise ControlToolError("不允许修改任务 key")
            for s in add_specs:
                if not valid_task_key(s.key):
                    raise ControlToolError(f"非法任务 key: {s.key}")
                if s.key in tasks:
                    raise ControlToolError(f"任务 key 已存在: {s.key}")
                agent = await self.registry.get_agent_by_slug(session, s.agent_slug)
                if agent is None or agent.status != AgentStatus.active:
                    raise ControlToolError(f"指派了不可用 agent: {s.agent_slug}")

            # 编队剧本：新增任务的阶段引用校验（D-4.5）
            bound = await session_team(session, run.session_id)
            stage_keys = playbook_stage_keys(bound[1]) if bound else []
            if stage_keys:
                for s in add_specs:
                    if s.playbook_stage_key and s.playbook_stage_key not in stage_keys:
                        raise ControlToolError(
                            f"任务 {s.key} 引用了剧本中不存在的阶段: {s.playbook_stage_key}"
                            f"（可用阶段: {', '.join(stage_keys)}）"
                        )
            elif any(s.playbook_stage_key for s in add_specs):
                raise ControlToolError("本会话未绑定团队剧本，不能填写 playbook_stage_key")

            # 应用变更
            for key in remove_keys:
                t = tasks[key]
                if not t.status.is_terminal:
                    await transition_task(
                        session, self.events, t, TaskStatus.cancelled,
                        reason=f"计划修订移除：{reason}", actor="orchestrator",
                    )
            for key, updates in update_specs.items():
                t = tasks[key]
                for field_name in ("title", "goal", "priority", "depends_on"):
                    if field_name in updates:
                        setattr(t, field_name, updates[field_name])
            added: list[Task] = []
            for s in add_specs:
                agent = await self.registry.get_agent_by_slug(session, s.agent_slug)
                assert agent is not None
                t = Task(
                    id=new_id("task"),
                    run_id=run.id,
                    key=s.key,
                    title=s.title,
                    goal=s.goal,
                    assigned_agent_id=agent.id,
                    depends_on=list(s.depends_on),
                    status=TaskStatus.pending,
                    priority=s.priority,
                    optional=s.optional,
                    playbook_stage_key=s.playbook_stage_key,
                )
                session.add(t)
                await session.flush()
                added.append(t)
                await self.events.emit(
                    session,
                    type="task.created",
                    actor="orchestrator",
                    run_id=run.id,
                    task_key=s.key,
                    payload={
                        "key": s.key, "title": s.title, "agent_slug": s.agent_slug,
                        "depends_on": list(s.depends_on),
                    },
                )

            all_keys = (set(tasks) - remove_keys) | {s.key for s in add_specs}
            _check_acyclic_keys(all_keys, self._deps_map(tasks, add_specs, update_specs))
            if len(all_keys) > int(settings["max_tasks_per_plan"]):
                raise ControlToolError(f"任务数超过上限 {settings['max_tasks_per_plan']}")

            version = run.current_plan_version + 1
            snapshot: list[dict[str, Any]] = []
            for t in list(tasks.values()) + added:
                if t.key in remove_keys:
                    continue
                agent = await self.registry.get_agent(session, t.assigned_agent_id)
                snapshot.append(
                    {
                        "key": t.key,
                        "title": t.title,
                        "goal": t.goal,
                        "agent_slug": agent.slug if agent else "",
                        "depends_on": list(t.depends_on),
                        "priority": t.priority,
                        "playbook_stage_key": t.playbook_stage_key,
                    }
                )
            plan = Plan(
                id=new_id("plan"),
                run_id=run.id,
                version=version,
                tasks_snapshot=snapshot,
                change_reason=reason,
                proposed_by="orchestrator",
                # awaiting 状态已在入口拒绝（审批门槛），到达此处即立即生效
                approved=True,
            )
            session.add(plan)
            await session.flush()
            run.current_plan_version = version
            await self.events.emit(
                session,
                type="plan.revised",
                actor="orchestrator",
                run_id=run.id,
                payload={"version": version, "reason": reason},
            )
            await record_decision(
                session, self.events, run_id=run.id, type=DecisionType.plan_revised,
                summary=f"修订计划 v{version}", rationale=reason,
            )

            # 修订导致预估费用超运行预算 50% 时必须重新确认（§5.6）
            estimated = await self._estimate_plan_cost(session, snapshot)
            run_budget = Budget(**(run.budget or {}))
            needs_reconfirm = (
                run_budget.max_cost is not None and estimated > run_budget.max_cost * 0.5
            )
            ready = await self._recompute_readiness(session, run)
            reconfirmed = False
            if needs_reconfirm and run.status == RunStatus.running:
                await transition_run(
                    session, self.events, run, RunStatus.awaiting_plan_approval,
                    reason=f"修订后预估费用 {estimated:.2f} 超过运行预算 50%", actor="orchestrator",
                )
                reconfirmed = True
        if ready:
            await self.wake(ws.run_id, "tasks_ready")
        await self._kick_scheduler()
        return {
            "ok": True,
            "plan_version": version,
            "ready_tasks": ready,
            "needs_reconfirmation": reconfirmed,
        }

    def _deps_map(
        self,
        tasks: dict[str, Task],
        add_specs: list[PlanTaskSpec],
        update_specs: dict[str, dict[str, Any]],
    ) -> dict[str, list[str]]:
        deps: dict[str, list[str]] = {}
        for key, t in tasks.items():
            if key in update_specs and "depends_on" in update_specs[key]:
                deps[key] = list(update_specs[key]["depends_on"])
            else:
                deps[key] = list(t.depends_on)
        for s in add_specs:
            deps[s.key] = list(s.depends_on)
        return {k: v for k, v in deps.items() if k not in {s.key for s in add_specs} or True}

    # --- 派发 ---
    async def _tool_dispatch(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        task_key = str(args.get("task_key") or "")
        async with db_session() as session:
            run = await self._require_run(session, ws.run_id)
            # 审批门槛：计划等待用户确认期间禁止派发（配合 revise_plan 同款门槛，
            # 否则任务排队而调度器不启动，形同卡死）
            if run.status == RunStatus.awaiting_plan_approval:
                raise ControlToolError("计划正在等待用户确认，不能派发任务")
            if run.id in self._budget_exceeded:
                raise ControlToolError("运行预算已耗尽：请用 finalize 汇总现有结果或上报用户追加预算")
            task = await self._require_task(session, ws.run_id, task_key)
            if task.status not in (TaskStatus.ready, TaskStatus.needs_rework, TaskStatus.blocked):
                raise ControlToolError(
                    f"任务 {task_key} 状态为 {task.status.value}，仅 ready/needs_rework/blocked 可派发"
                )

            agent_slug = str(args.get("agent_slug") or "")
            agent = await self.registry.get_agent_by_slug(session, agent_slug)
            if agent is None or agent.status != AgentStatus.active:
                raise ControlToolError(f"agent 不可用: {agent_slug}")
            if agent.id != task.assigned_agent_id:
                raise ControlToolError(
                    f"任务 {task_key} 当前负责人不是 {agent_slug}，请先 reassign"
                )

            # 协调硬约束（§5.7）
            last_blocker = await self._last_blocker_type(session, task)
            if (
                task.status == TaskStatus.blocked
                and last_blocker in (BlockerType.missing_tool, BlockerType.out_of_scope)
                and args.get("allow_same_agent") is not True
            ):
                prev_agent = await self._last_attempt_agent(session, task)
                if prev_agent is not None and prev_agent.id == agent.id:
                    raise ControlToolError(
                        f"任务因 {last_blocker.value} 受阻，不能原样重派给同一 agent（{agent_slug}）"
                    )

            if task.attempt_count >= task.max_attempts:
                raise ControlToolError(
                    f"任务 {task_key} 已达最大尝试次数 {task.max_attempts}，应标记失败或上报用户"
                )

            # 死循环防护（§11.3）：连续两次返工内容高度相似则阻止第三次
            if task.status == TaskStatus.needs_rework:
                similar = await self._rework_similarity(session, task)
                if similar is not None and similar > MAX_REWORK_SIMILARITY:
                    raise ControlToolError(
                        "检测到返工内容与上次高度相似（>90%），已阻止再次返工；请上报用户处理"
                    )

            criteria = [str(c) for c in args.get("acceptance_criteria") or []]
            if not criteria:
                raise ControlToolError("acceptance_criteria 至少 1 条")
            context = str(args.get("context") or "")
            if len(context) > 3000:
                raise ControlToolError("context 超过 3000 字上限")

            # 输入引用必须存在
            input_refs = []
            for i in args.get("inputs") or []:
                art = await self._artifact_or_none(session, str(i.get("artifact_id")))
                if art is None:
                    raise ControlToolError(f"inputs 引用的交付物不存在: {i.get('artifact_id')}")
                input_refs.append({"artifact_id": art.id, "usage": str(i.get("usage") or "")})

            budget_dict = args.get("budget") or None
            brief_budget = Budget(**budget_dict) if budget_dict else None
            if brief_budget is None:
                brief_budget = Budget(**(agent.default_budget or {}))
            run_budget = Budget(**(run.budget or {}))
            run_usage = Usage(**(run.usage or {}))
            remaining_cost = (
                run_budget.max_cost - run_usage.cost
                if run_budget.max_cost is not None
                else None
            )
            if (
                brief_budget.max_cost is not None
                and remaining_cost is not None
                and brief_budget.max_cost > remaining_cost
            ):
                brief_budget = brief_budget.model_copy(
                    update={"max_cost": max(remaining_cost, 0.0)}
                )

            deliverable_dict = args.get("deliverable") or {}
            if not deliverable_dict and args.get("deliverable_format"):
                deliverable_dict = {
                    "format": args.get("deliverable_format"),
                    "name": args.get("deliverable_name") or "",
                    "notes": args.get("deliverable_notes") or "",
                }
            if not deliverable_dict:
                raise ControlToolError("缺少 deliverable 交付要求")

            last_attempt = await self._latest_attempt(session, task)
            rework_of = last_attempt.id if task.status == TaskStatus.needs_rework and last_attempt else None
            rework_feedback = (
                str(args.get("rework_feedback") or "")
                if task.status == TaskStatus.needs_rework
                else None
            )

            brief = TaskBrief(
                task_key=task.key,
                agent_slug=agent.slug,
                objective=str(args.get("objective") or ""),
                context=context,
                inputs=input_refs,  # type: ignore[arg-type]
                constraints=[str(c) for c in args.get("constraints") or []],
                acceptance_criteria=criteria,
                deliverable=deliverable_dict,  # type: ignore[arg-type]
                budget=brief_budget,
                priority=int(args.get("priority") or task.priority),
                selection_rationale=str(args.get("selection_rationale") or ""),
                rework_of=rework_of,
                rework_feedback=rework_feedback,
            )

            number = (last_attempt.number if last_attempt else 0) + 1
            attempt = Attempt(
                id=new_id("att"),
                task_id=task.id,
                number=number,
                agent_id=agent.id,
                agent_version=agent.version,
                brief=brief.model_dump(mode="json"),
                status="queued",
                counted=True,
            )
            session.add(attempt)
            await session.flush()

            task.attempt_count += 1
            await transition_task(
                session, self.events, task, TaskStatus.queued,
                reason="任务单已提交", actor="orchestrator",
            )
            # 首次派发即进入执行期：驳回-修订后未经 approve 的计划，
            # run 若仍停留 planning，scheduler 不会启动尝试（真实场景死队）
            if run.status == RunStatus.planning:
                await transition_run(
                    session, self.events, run, RunStatus.running,
                    reason=f"计划 v{run.current_plan_version} 派发中", actor="orchestrator",
                )
            await self.events.emit(
                session,
                type="attempt.queued",
                actor="orchestrator",
                run_id=ws.run_id,
                task_key=task.key,
                attempt_id=attempt.id,
                payload={"task_key": task.key, "number": number, "agent_slug": agent.slug},
            )
            await record_decision(
                session,
                self.events,
                run_id=ws.run_id,
                type=DecisionType.dispatched,
                task_id=task.id,
                task_key=task.key,
                summary=f"派发 {task.key} 给 {agent.slug}（第 {number} 次尝试）",
                rationale=str(args.get("rationale") or ""),
                inputs=[i["artifact_id"] for i in input_refs],
            )
            if rework_of:
                await record_decision(
                    session,
                    self.events,
                    run_id=ws.run_id,
                    type=DecisionType.retried,
                    task_id=task.id,
                    task_key=task.key,
                    summary=f"{task.key} 返工重派",
                    rationale=str(args.get("rework_feedback") or args.get("rationale") or ""),
                )
        await self._kick_scheduler()
        return {"ok": True, "attempt_number": number, "task_key": task_key}

    # --- 复核 ---
    async def _tool_review(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        attempt_id = str(args.get("attempt_id") or "")
        task_key_ref = str(args.get("task_key") or "")
        review_data = args.get("review") or {}
        review = Review(**review_data)
        verdict = review.verdict

        async with db_session() as session:
            run = await self._require_run(session, ws.run_id)
            attempt: Attempt | None = None
            task: Task | None = None
            if attempt_id:
                result = await session.execute(select(Attempt).where(Attempt.id == attempt_id))
                attempt = result.scalar_one_or_none()
                task = await self._task_by_id(session, attempt.task_id) if attempt else None
            elif task_key_ref:
                task = await self._require_task(session, ws.run_id, task_key_ref)
                result = await session.execute(
                    select(Attempt)
                    .where(Attempt.task_id == task.id, Attempt.status == "reported")
                    .order_by(Attempt.number.desc())
                    .limit(1)
                )
                attempt = result.scalar_one_or_none()
            if attempt is None or task is None:
                raise ControlToolError("找不到待复核的尝试（attempt_id/task_key）")
            if task.status not in (TaskStatus.reported, TaskStatus.blocked):
                raise ControlToolError(
                    f"任务 {task.key} 不处于待复核状态（{task.status.value}）"
                )
            if task.status == TaskStatus.blocked and verdict != "fail":
                raise ControlToolError(
                    f"任务 {task.key} 处于 blocked，只接受 fail 结论（改派/重派走 dispatch）"
                )

            report = attempt.report or {}
            brief_criteria = [c for c in (attempt.brief or {}).get("acceptance_criteria") or []]
            covered = {c.criterion for c in review.criteria}
            missing = [c for c in brief_criteria if c not in covered]
            if missing:
                raise ControlToolError(
                    f"复核必须逐条给出结论，缺少: {missing[:3]}"
                )

            # 复核必须实际读取交付物（§5.9）
            artifacts = await self._attempt_artifacts(session, attempt.id)
            if verdict in ("pass", "pass_with_gaps"):
                if artifacts and not (set(a.id for a in artifacts) & ws.read_artifact_ids):
                    raise ControlToolError(
                        "复核通过前必须先 read_artifact 阅读交付物（本次唤醒内未读取）"
                    )

            # 采纳 partial 必须写明缺口（§5.7）
            if (
                verdict == "pass_with_gaps"
                and report.get("status") == ReportStatus.partial.value
                and not review.gaps_note.strip()
            ):
                raise ControlToolError("采纳 partial 回报必须填写 gaps_note 缺口说明")

            attempt.review = review.model_dump(mode="json")
            await self.events.emit(
                session,
                type="review.submitted",
                actor="orchestrator",
                run_id=ws.run_id,
                task_key=task.key,
                attempt_id=attempt.id,
                payload={
                    "task_key": task.key,
                    "attempt_id": attempt.id,
                    "verdict": verdict,
                    "review": review.model_dump(mode="json"),
                },
            )

            if verdict == "fail":
                # 报告 status=failed（agent 确认做不到）或已达最大尝试次数 → 终态 failed（D-1.x）
                if task.attempt_count >= task.max_attempts or (
                    (attempt.report or {}).get("status") == ReportStatus.failed.value
                ):
                    await transition_task(
                        session, self.events, task, TaskStatus.failed,
                        reason=f"复核不通过且判定不可完成（{review.required_changes[:100]}）",
                        actor="orchestrator",
                    )
                else:
                    await transition_task(
                        session, self.events, task, TaskStatus.needs_rework,
                        reason="复核不通过", actor="orchestrator",
                    )
                await record_decision(
                    session, self.events, run_id=ws.run_id, type=DecisionType.review_failed,
                    task_id=task.id, task_key=task.key,
                    summary=f"{task.key} 复核不通过（{verdict}）",
                    rationale=str(args.get("rationale") or ""),
                )
                ready: list[str] = []
            else:
                await transition_task(
                    session, self.events, task, TaskStatus.accepted,
                    reason="复核通过" if verdict == "pass" else "复核通过（有缺口）",
                    actor="orchestrator",
                )
                task.result_summary = str(report.get("summary") or "")[:500]
                task.result_artifact_ids = [a.id for a in artifacts]
                for a in artifacts:
                    a.status = ArtifactStatus("accepted")
                    await self.events.emit(
                        session,
                        type="artifact.status_changed",
                        actor="orchestrator",
                        run_id=ws.run_id,
                        task_key=task.key,
                        payload={
                            "artifact_id": a.id,
                            "from_status": "submitted",
                            "to_status": "accepted",
                        },
                    )
                await record_decision(
                    session, self.events, run_id=ws.run_id, type=DecisionType.review_passed,
                    task_id=task.id, task_key=task.key,
                    summary=f"{task.key} 复核通过（{verdict}）",
                    rationale=str(args.get("rationale") or "") if verdict == "pass" else review.gaps_note,
                )
                ready = await self._recompute_readiness(session, run)
        if verdict != "fail" and ready:
            await self.wake(ws.run_id, "tasks_ready")
        await self._post_orchestrate(ws.run_id)
        return {"ok": True, "verdict": verdict}

    # --- 改派 / 拆分 / 提问 / 跳过 ---
    async def _tool_reassign(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        task_key = str(args.get("task_key") or "")
        agent_slug = str(args.get("agent_slug") or "")
        async with db_session() as session:
            await self._require_run(session, ws.run_id)
            task = await self._require_task(session, ws.run_id, task_key)
            if task.status.is_terminal:
                raise ControlToolError(f"任务 {task_key} 已终态")
            agent = await self.registry.get_agent_by_slug(session, agent_slug)
            if agent is None or agent.status != AgentStatus.active:
                raise ControlToolError(f"agent 不可用: {agent_slug}")
            prev_agent = await self.registry.get_agent(session, task.assigned_agent_id)

            # 同一任务改派达 2 次必须上报（§5.7）
            reassign_count = await self._count_decisions(
                session, ws.run_id, DecisionType.reassigned, task.id
            )
            if reassign_count >= 2:
                raise ControlToolError(
                    f"任务 {task_key} 已改派 {reassign_count} 次，必须先 ask_user 上报用户"
                )
            if prev_agent is not None and prev_agent.id == agent.id:
                raise ControlToolError("不能改派给当前负责人")

            task.assigned_agent_id = agent.id
            await session.flush()
            await self.events.emit(
                session,
                type="task.reassigned",
                actor="orchestrator",
                run_id=ws.run_id,
                task_key=task.key,
                payload={
                    "key": task.key,
                    "from_agent": prev_agent.slug if prev_agent else "",
                    "to_agent": agent.slug,
                    "reason": str(args.get("reason") or ""),
                },
            )
            await record_decision(
                session, self.events, run_id=ws.run_id, type=DecisionType.reassigned,
                task_id=task.id, task_key=task.key,
                summary=f"{task.key} 改派给 {agent.slug}",
                rationale=str(args.get("reason") or args.get("rationale") or ""),
                alternatives=[prev_agent.slug] if prev_agent else [],
            )
        return {"ok": True}

    async def _tool_split(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        task_key = str(args.get("task_key") or "")
        subtasks = [PlanTaskSpec(**t) for t in args.get("subtasks") or []]
        if len(subtasks) < 2:
            raise ControlToolError("拆分至少产生 2 个子任务")
        async with db_session() as session:
            run = await self._require_run(session, ws.run_id)
            task = await self._require_task(session, ws.run_id, task_key)
            if task.status.is_terminal:
                raise ControlToolError(f"任务 {task_key} 已终态")
            tasks = {t.key: t for t in await self._run_tasks(session, ws.run_id)}
            for s in subtasks:
                if s.key in tasks or any(s.key == x.key for x in subtasks if x is not s):
                    raise ControlToolError(f"子任务 key 冲突: {s.key}")
                agent = await self.registry.get_agent_by_slug(session, s.agent_slug)
                if agent is None or agent.status != AgentStatus.active:
                    raise ControlToolError(f"子任务 {s.key} 指派了不可用 agent")

            sub_keys = [s.key for s in subtasks]
            # 下游依赖继承：依赖原任务的任务改为依赖全部子任务
            for t in tasks.values():
                if task.key in t.depends_on:
                    t.depends_on = [
                        d for d in t.depends_on if d != task.key
                    ] + [k for k in sub_keys if k not in t.depends_on]

            await transition_task(
                session, self.events, task, TaskStatus.superseded,
                reason="被拆分", actor="orchestrator",
            )
            created: list[Task] = []
            for s in subtasks:
                agent = await self.registry.get_agent_by_slug(session, s.agent_slug)
                assert agent is not None
                sub = Task(
                    id=new_id("task"),
                    run_id=run.id,
                    key=s.key,
                    title=s.title,
                    goal=s.goal,
                    assigned_agent_id=agent.id,
                    depends_on=list(s.depends_on) or list(task.depends_on),
                    status=TaskStatus.pending,
                    priority=s.priority,
                    parent_task_key=task.key,
                    optional=s.optional,
                )
                session.add(sub)
                await session.flush()
                created.append(sub)
                await self.events.emit(
                    session,
                    type="task.created",
                    actor="orchestrator",
                    run_id=run.id,
                    task_key=s.key,
                    payload={
                        "key": s.key, "title": s.title, "agent_slug": s.agent_slug,
                        "depends_on": list(sub.depends_on),
                    },
                )
            await self.events.emit(
                session,
                type="task.split",
                actor="orchestrator",
                run_id=run.id,
                task_key=task.key,
                payload={"key": task.key, "subtask_keys": sub_keys,
                         "reason": str(args.get("rationale") or "")},
            )
            await record_decision(
                session, self.events, run_id=run.id, type=DecisionType.split_task,
                task_id=task.id, task_key=task.key,
                summary=f"{task.key} 拆分为 {sub_keys}",
                rationale=str(args.get("rationale") or ""),
            )
            ready = await self._recompute_readiness(session, run)
        if ready:
            await self.wake(ws.run_id, "tasks_ready")
        return {"ok": True, "subtask_keys": sub_keys, "ready_tasks": ready}

    async def _tool_ask_user(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "")
        if not question:
            raise ControlToolError("question 不能为空")
        options = [str(o) for o in args.get("options") or []]
        blocking = [str(k) for k in args.get("blocking_task_keys") or []]
        question_id = new_id("q")

        async with db_session() as session:
            run = await self._require_run(session, ws.run_id)
            for key in blocking:
                t = await self._require_task(session, ws.run_id, key)
                if t.status not in (TaskStatus.blocked, TaskStatus.reported):
                    raise ControlToolError(
                        f"任务 {key} 状态 {t.status.value} 不可被提问阻塞（仅 blocked/reported）"
                    )
                await transition_task(
                    session, self.events, t, TaskStatus.awaiting_user,
                    reason="等待用户回答", actor="orchestrator",
                )
            await self.events.emit(
                session,
                type="user.question_asked",
                actor="orchestrator",
                run_id=ws.run_id,
                payload={
                    "question_id": question_id,
                    "question": question,
                    "options": options,
                    "blocking_task_keys": blocking,
                },
            )
            await record_decision(
                session, self.events, run_id=ws.run_id, type=DecisionType.asked_user,
                summary=f"向用户提问：{question[:80]}",
                rationale=str(args.get("rationale") or ""),
            )
            self._questions.setdefault(ws.run_id, {})[question_id] = PendingQuestion(
                question_id, question, options, blocking
            )
            if not blocking:
                if run.status == RunStatus.planning:
                    await transition_run(
                        session, self.events, run, RunStatus.clarifying,
                        reason="计划前澄清", actor="orchestrator",
                    )
            else:
                if run.status == RunStatus.planning:
                    await transition_run(
                        session, self.events, run, RunStatus.clarifying,
                        reason="计划前澄清", actor="orchestrator",
                    )
                tasks = await self._run_tasks(session, ws.run_id)
                non_terminal = [t for t in tasks if not t.status.is_terminal]
                if non_terminal and all(
                    t.status in (TaskStatus.awaiting_user,) for t in non_terminal
                ):
                    await transition_run(
                        session, self.events, run, RunStatus.awaiting_user,
                        reason="全部可推进任务等待用户", actor="orchestrator",
                    )
        return {"ok": True, "question_id": question_id}

    async def _tool_read_artifact(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        artifact_id = str(args.get("artifact_id") or "")
        task_key_ref = str(args.get("task_key") or "")
        mode = str(args.get("mode") or "summary")
        from app.artifacts.repo import ArtifactRepository

        repo = ArtifactRepository(self.events)
        async with db_session() as session:
            artifact = None
            if artifact_id:
                artifact = await repo.get(session, artifact_id)
                if artifact is None:
                    raise ControlToolError(f"交付物不存在: {artifact_id}")
            elif task_key_ref:
                task = await self._require_task(session, ws.run_id, task_key_ref)
                from sqlalchemy import select as sa_select

                result = await session.execute(
                    sa_select(Artifact)
                    .where(Artifact.task_id == task.id)
                    .order_by(Artifact.created_at.desc())
                    .limit(1)
                )
                artifact = result.scalar_one_or_none()
                if artifact is None:
                    raise ControlToolError(f"任务 {task_key_ref} 尚无交付物")
            else:
                raise ControlToolError("必须提供 artifact_id 或 task_key")
            if mode == "summary":
                content = artifact.summary
            else:
                content = await repo.read_content(
                    artifact,
                    mode,
                    offset=int(args.get("offset") or 0),
                    length=int(args.get("length") or 0),
                )
            ws.read_artifact_ids.add(artifact.id)
            task_key = ""
            if artifact.task_id:
                t = await self._task_by_id(session, artifact.task_id)
                task_key = t.key if t else ""
            return {
                "artifact_id": artifact.id,
                "task_key": task_key,
                "name": artifact.name,
                "kind": artifact.kind.value,
                "summary": artifact.summary,
                "content": content,
            }

    async def _tool_skip(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        task_key = str(args.get("task_key") or "")
        async with db_session() as session:
            run = await self._require_run(session, ws.run_id)
            task = await self._require_task(session, ws.run_id, task_key)
            if task.status.is_terminal:
                raise ControlToolError(f"任务 {task_key} 已终态")
            tasks = await self._run_tasks(session, ws.run_id)
            dependents = [t.key for t in tasks if task.key in t.depends_on
                          and not t.status.is_terminal]
            if not task.optional and dependents:
                raise ControlToolError(
                    f"任务 {task_key} 是关键任务（被 {dependents} 依赖），需先 ask_user 征得用户同意"
                )
            await transition_task(
                session, self.events, task, TaskStatus.skipped,
                reason=str(args.get("rationale") or ""), actor="orchestrator",
            )
            await self.events.emit(
                session,
                type="task.skipped",
                actor="orchestrator",
                run_id=ws.run_id,
                task_key=task.key,
                payload={"key": task.key, "reason": str(args.get("rationale") or "")},
            )
            await record_decision(
                session, self.events, run_id=ws.run_id, type=DecisionType.skipped,
                task_id=task.id, task_key=task.key,
                summary=f"跳过 {task.key}", rationale=str(args.get("rationale") or ""),
            )
            ready = await self._recompute_readiness(session, run)
        if ready:
            await self.wake(ws.run_id, "tasks_ready")
        await self._post_orchestrate(ws.run_id)
        return {"ok": True}

    async def _tool_finalize(self, ws: WakeState, args: dict[str, Any]) -> dict[str, Any]:
        answer = str(args.get("answer") or "")
        if not answer:
            raise ControlToolError("answer 不能为空")
        citations = args.get("citations") or []
        gaps = str(args.get("gaps") or "")

        async with db_session() as session:
            run = await self._require_run(session, ws.run_id)
            tasks = await self._run_tasks(session, ws.run_id)
            if tasks and not all(t.status.is_terminal for t in tasks):
                raise ControlToolError("仍有任务未到终态，不能 finalize")
            for c in citations:
                key = str(c.get("task_key") or "")
                if key and key not in {t.key for t in tasks}:
                    raise ControlToolError(f"citation 引用了不存在的任务: {key}")
                art_id = c.get("artifact_id")
                if art_id:
                    art = await self._artifact_or_none(session, str(art_id))
                    if art is None:
                        raise ControlToolError(f"citation 引用了不存在的交付物: {art_id}")
                    if art.status.value != "accepted":
                        raise ControlToolError(
                            f"最终答复引用的交付物必须是 accepted 状态: {art_id}"
                        )

            failed = [t for t in tasks if t.status == TaskStatus.failed]
            skipped = [t for t in tasks if t.status == TaskStatus.skipped]
            accepted = [t for t in tasks if t.status == TaskStatus.accepted]
            if not accepted and tasks:
                outcome = RunOutcome.failed
            elif failed or skipped or gaps.strip():
                outcome = RunOutcome.partial
            else:
                # 零任务（直接答复型需求）同样视为成功完成
                outcome = RunOutcome.success

            gap_lines = [gaps] if gaps.strip() else []
            for t in failed:
                gap_lines.append(f"- {t.key}「{t.title}」失败：{t.result_summary or '未达成'}")
            for t in skipped:
                gap_lines.append(f"- {t.key}「{t.title}」被跳过")

            run.final_answer = answer
            run.outcome = outcome
            run.summary_of_gaps = "\n".join(gap_lines)
            await transition_run(
                session, self.events, run, RunStatus.completed,
                reason="最终答复已提交", actor="orchestrator",
            )
            usage = Usage(**(run.usage or {}))
            await self.events.emit(
                session,
                type="run.completed",
                actor="orchestrator",
                run_id=ws.run_id,
                payload={
                    "outcome": outcome.value,
                    "final_answer": answer,
                    "gaps": run.summary_of_gaps,
                    "usage": usage.model_dump(mode="json"),
                },
            )
            await record_decision(
                session, self.events, run_id=ws.run_id, type=DecisionType.finalized,
                summary=f"汇总答复（{outcome.value}）", rationale=str(args.get("rationale") or ""),
            )
            # 会话消息流中记录最终答复
            session.add(
                Message(
                    id=new_id("msg"),
                    session_id=run.session_id,
                    role="orchestrator",
                    content=answer,
                    run_id=run.id,
                )
            )
        return {"ok": True, "outcome": outcome.value}

    # ------------------------------------------------------------------
    # 用户侧操作（API 调用）
    # ------------------------------------------------------------------
    async def approve_plan(
        self, run_id: str, version: int, modifications: dict[str, Any] | None = None
    ) -> None:
        ready: list[str] = []
        async with db_session() as session:
            run = await self._require_run(session, run_id)
            await self._approve_plan_internal(
                session, run, version, by="user", modifications=modifications
            )
            ready = await self._recompute_readiness(session, run)
        if ready:
            await self.wake(run_id, "tasks_ready")
        await self._kick_scheduler()

    async def _approve_plan_internal(
        self,
        session: AsyncSession,
        run: Run,
        version: int,
        *,
        by: str,
        modifications: dict[str, Any] | None = None,
    ) -> None:
        result = await session.execute(
            select(Plan).where(Plan.run_id == run.id, Plan.version == version)
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            raise ControlToolError(f"计划版本不存在: v{version}")
        if plan.approved:
            return
        plan.approved = True
        run.current_plan_version = version
        if run.status in (RunStatus.awaiting_plan_approval, RunStatus.planning):
            await transition_run(
                session, self.events, run, RunStatus.running,
                reason=f"计划 v{version} 已确认", actor="user" if by == "user" else "system",
            )
        await self.events.emit(
            session,
            type="plan.approved",
            actor="user" if by == "user" else "system",
            run_id=run.id,
            payload={"version": version, "by": by,
                     "modifications": modifications or {}},
        )

    async def reject_plan(self, run_id: str, version: int, note: str) -> None:
        async with db_session() as session:
            run = await self._require_run(session, run_id)
            # 驳回理由落为 user 消息：主 agent 下次唤醒经 digest 时间线可见（否则模型不知要改什么）
            session.add(
                Message(
                    id=new_id("msg"),
                    session_id=run.session_id,
                    role="user",
                    content=f"【计划被驳回】v{version}：{note}",
                    run_id=run.id,
                )
            )
            await self.events.emit(
                session,
                type="plan.rejected",
                actor="user",
                run_id=run.id,
                payload={"version": version, "note": note},
            )
            if run.status == RunStatus.awaiting_plan_approval:
                await transition_run(
                    session, self.events, run, RunStatus.planning,
                    reason=f"计划 v{version} 被驳回：{note}", actor="user",
                )
        await self.wake(run_id, "plan_rejected")

    async def submit_answer(self, run_id: str, answer: str, question_id: str | None = None) -> None:
        async with db_session() as session:
            run = await self._require_run(session, run_id)
            questions = self._questions.get(run_id, {})
            qid = question_id or (list(questions.keys())[-1] if questions else None)
            q_text = ""
            if qid is not None and qid in questions:
                q = questions.pop(qid)
                q_text = q.question
                for key in q.blocking_task_keys:
                    t = await self._require_task(session, run_id, key)
                    if t.status == TaskStatus.awaiting_user:
                        await transition_task(
                            session, self.events, t, TaskStatus.ready,
                            reason="用户已回答", actor="user",
                        )
                await self.events.emit(
                    session,
                    type="user.answered",
                    actor="user",
                    run_id=run_id,
                    payload={"question_id": qid, "answer": answer},
                )
            # 回答落为 user 消息：主 agent 下次唤醒经 digest 时间线可见（否则模型不知用户答了什么）
            session.add(
                Message(
                    id=new_id("msg"),
                    session_id=run.session_id,
                    role="user",
                    content=f"【用户回答】{'问：' + q_text + ' 答：' if q_text else '答：'}{answer}",
                    run_id=run_id,
                )
            )
            if run.status == RunStatus.clarifying:
                await transition_run(
                    session, self.events, run, RunStatus.planning,
                    reason="用户已回答澄清问题", actor="user",
                )
            elif run.status == RunStatus.awaiting_user:
                await transition_run(
                    session, self.events, run, RunStatus.running,
                    reason="用户已回答", actor="user",
                )
        await self.wake(run_id, "user_answered")

    async def pause_run(self, run_id: str) -> None:
        async with db_session() as session:
            run = await self._require_run(session, run_id)
            if run.status == RunStatus.running:
                await transition_run(
                    session, self.events, run, RunStatus.paused, reason="用户暂停", actor="user"
                )
        if self.scheduler is not None:
            await self.scheduler.pause_run(run_id)

    async def resume_run(self, run_id: str) -> None:
        async with db_session() as session:
            run = await self._require_run(session, run_id)
            if run.status == RunStatus.paused:
                await transition_run(
                    session, self.events, run, RunStatus.running, reason="用户继续", actor="user"
                )
        if self.scheduler is not None:
            await self.scheduler.resume_run(run_id)
        await self.wake(run_id, "resumed")
        await self._kick_scheduler()

    async def cancel_run(self, run_id: str, reason: str = "") -> None:
        if self.scheduler is not None:
            await self.scheduler.interrupt_run(run_id)
        async with db_session() as session:
            run = await self._require_run(session, run_id)
            for t in await self._run_tasks(session, run_id):
                if not t.status.is_terminal:
                    await transition_task(
                        session, self.events, t, TaskStatus.cancelled,
                        reason=f"运行取消：{reason}", actor="user",
                    )
            if not run.status.is_terminal:
                await transition_run(
                    session, self.events, run, RunStatus.cancelled,
                    reason=reason or "用户取消", actor="user",
                )
                run.outcome = RunOutcome.cancelled
                await self.events.emit(
                    session,
                    type="run.cancelled",
                    actor="user",
                    run_id=run_id,
                    payload={"reason": reason},
                )

    async def extend_budget(self, run_id: str, extra: Budget) -> None:
        async with db_session() as session:
            run = await self._require_run(session, run_id)
            current = Budget(**(run.budget or {}))
            data = current.model_dump()
            for key, value in extra.model_dump().items():
                if value is not None:
                    old = data.get(key)
                    data[key] = (old or 0) + value if key == "max_cost" else value
            run.budget = data
            await self.events.emit(
                session,
                type="budget.extended",
                actor="user",
                run_id=run_id,
                payload={"new_budget": data},
            )
        self._budget_exceeded.discard(run_id)
        self._budget_warned.discard(run_id)
        await self.wake(run_id, "budget_extended")

    # ------------------------------------------------------------------
    # 就绪度与收尾
    # ------------------------------------------------------------------
    async def _recompute_readiness(self, session: AsyncSession, run: Run) -> list[str]:
        """pending 任务的依赖全部 accepted/skipped → ready；有 failed/cancelled → blocked。"""
        tasks = await self._run_tasks(session, run.id)
        by_key = {t.key: t for t in tasks}
        ready: list[str] = []
        blocked: list[tuple[Task, str]] = []
        for t in tasks:
            if t.status != TaskStatus.pending or not t.depends_on:
                if t.status == TaskStatus.pending and not t.depends_on:
                    ready.append(t.key)
                    await transition_task(
                        session, self.events, t, TaskStatus.ready,
                        reason="无依赖", actor="system",
                    )
                continue
            dep_status = [by_key[d].status if d in by_key else None for d in t.depends_on]
            if any(d is None for d in dep_status):
                blocked.append((t, "依赖的任务不存在"))
            elif any(d in (TaskStatus.failed, TaskStatus.cancelled) for d in dep_status):
                blocked.append((t, "上游任务 failed/cancelled（needs_collaboration）"))
            elif all(d in (TaskStatus.accepted, TaskStatus.skipped) for d in dep_status):
                ready.append(t.key)
                await transition_task(
                    session, self.events, t, TaskStatus.ready,
                    reason="依赖已满足", actor="system",
                )
        for t, why in blocked:
            await transition_task(
                session, self.events, t, TaskStatus.blocked, reason=why, actor="system"
            )
        return ready

    async def _check_synthesizing(self, session: AsyncSession, run: Run) -> None:
        if run.status != RunStatus.running:
            return
        tasks = await self._run_tasks(session, run.id)
        if tasks and all(t.status.is_terminal for t in tasks):
            await transition_run(
                session, self.events, run, RunStatus.synthesizing, reason="全部任务终态"
            )

    # ------------------------------------------------------------------
    # 预算跟踪
    # ------------------------------------------------------------------
    async def _track_orchestrator_usage(
        self, run_id: str, usage: Usage, cfg: ModelConfig
    ) -> None:
        delta = Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost=cfg.cost_of(usage),
        )
        await self.accumulate_usage(run_id, delta)

    async def accumulate_usage(self, run_id: str, delta: Usage) -> None:
        async with db_session() as session:
            run = await self._load_run(session, run_id)
            if run is None:
                return
            usage = Usage(**(run.usage or {}))
            before = usage.ratio(Budget(**(run.budget or {})))
            usage.add(delta)
            run.usage = usage.model_dump(mode="json")
            budget = Budget(**(run.budget or {}))
            after = usage.ratio(budget)
            if after is not None:
                if after >= 1.0 and run_id not in self._budget_exceeded:
                    self._budget_exceeded.add(run_id)
                    await self.events.emit(
                        session,
                        type="budget.exceeded",
                        actor="system",
                        run_id=run_id,
                        payload={"level": "run", "usage": usage.model_dump(mode="json")},
                    )
                    await self.wake(run_id, "budget_exceeded")
                elif after >= 0.8 and run_id not in self._budget_warned:
                    self._budget_warned.add(run_id)
                    await self.events.emit(
                        session,
                        type="budget.warning",
                        actor="system",
                        run_id=run_id,
                        payload={"level": "run", "ratio": after,
                                 "usage": usage.model_dump(mode="json")},
                    )
            _ = before

    # ------------------------------------------------------------------
    # 恢复（§5.10）
    # ------------------------------------------------------------------
    async def recover(self) -> None:
        """服务重启后恢复：续跑或标记崩溃、重新调度、唤醒未完成运行。"""
        async with db_session() as session:
            running_attempts = (
                await session.execute(select(Attempt).where(Attempt.status == "running"))
            ).scalars().all()
            crashed: list[Attempt] = []
            for attempt in running_attempts:
                crashed.append(attempt)
            unfinished_runs = (
                await session.execute(
                    select(Run).where(
                        Run.status.in_(
                            [
                                RunStatus.planning,
                                RunStatus.clarifying,
                                RunStatus.running,
                                RunStatus.synthesizing,
                                RunStatus.awaiting_user,
                            ]
                        )
                    )
                )
            ).scalars().all()
            await self.events.emit(
                session,
                type="engine.recovered",
                actor="system",
                run_id=None,
                payload={"detail": f"恢复检查：{len(crashed)} 个运行中尝试，{len(unfinished_runs)} 个未完成运行"},
            )
        for attempt in crashed:
            if self.scheduler is not None:
                await self.scheduler.handle_crashed_attempt(attempt.id, "服务重启，无法续跑")
        for run in unfinished_runs:
            await self.wake(run.id, "engine_recovered")
        await self._kick_scheduler()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    async def _load_run(self, session: AsyncSession, run_id: str) -> Run | None:
        result = await session.execute(select(Run).where(Run.id == run_id))
        return result.scalar_one_or_none()

    async def _require_run(self, session: AsyncSession, run_id: str) -> Run:
        run = await self._load_run(session, run_id)
        if run is None:
            raise ControlToolError(f"运行不存在: {run_id}")
        return run

    async def _run_tasks(self, session: AsyncSession, run_id: str) -> list[Task]:
        result = await session.execute(
            select(Task).where(Task.run_id == run_id).order_by(Task.key)
        )
        return list(result.scalars().all())

    async def _require_task(self, session: AsyncSession, run_id: str, key: str) -> Task:
        result = await session.execute(
            select(Task).where(Task.run_id == run_id, Task.key == key)
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise ControlToolError(f"任务不存在: {key}")
        return task

    async def _task_by_id(self, session: AsyncSession, task_id: str) -> Task | None:
        result = await session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one_or_none()

    async def _artifact_or_none(self, session: AsyncSession, artifact_id: str) -> Artifact | None:
        result = await session.execute(select(Artifact).where(Artifact.id == artifact_id))
        return result.scalar_one_or_none()

    async def _attempt_artifacts(self, session: AsyncSession, attempt_id: str) -> list[Artifact]:
        result = await session.execute(
            select(Artifact).where(Artifact.attempt_id == attempt_id)
        )
        return list(result.scalars().all())

    async def _latest_attempt(self, session: AsyncSession, task: Task) -> Attempt | None:
        result = await session.execute(
            select(Attempt)
            .where(Attempt.task_id == task.id)
            .order_by(Attempt.number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _last_attempt_agent(self, session: AsyncSession, task: Task) -> Agent | None:
        attempt = await self._latest_attempt(session, task)
        if attempt is None:
            return None
        return await self.registry.get_agent(session, attempt.agent_id)

    async def _last_blocker_type(self, session: AsyncSession, task: Task) -> BlockerType | None:
        attempt = await self._latest_attempt(session, task)
        if attempt is None or not attempt.report:
            return None
        blocker = (attempt.report or {}).get("blocker")
        if not blocker:
            return None
        try:
            return BlockerType(blocker.get("type"))
        except ValueError:
            return None

    async def _rework_similarity(self, session: AsyncSession, task: Task) -> float | None:
        """最近两次已回报尝试的回报摘要 + 任务单目标相似度（§11.3）。"""
        result = await session.execute(
            select(Attempt)
            .where(Attempt.task_id == task.id, Attempt.report.is_not(None))
            .order_by(Attempt.number.desc())
            .limit(2)
        )
        attempts = list(result.scalars().all())
        if len(attempts) < 2:
            return None
        a, b = attempts[0], attempts[1]

        def sig(x: Attempt) -> str:
            report = x.report or {}
            return f"{report.get('summary', '')}|{(x.brief or {}).get('objective', '')}"

        return text_similarity(sig(a), sig(b))

    async def _count_decisions(
        self, session: AsyncSession, run_id: str, type: DecisionType, task_id: str
    ) -> int:
        from sqlalchemy import func

        result = await session.execute(
            select(func.count())
            .select_from(Decision)
            .where(
                Decision.run_id == run_id,
                Decision.type == type,
                Decision.task_id == task_id,
            )
        )
        return int(result.scalar_one_or_none() or 0)

    async def _estimate_plan_cost(
        self, session: AsyncSession, specs: list[dict[str, Any]]
    ) -> float:
        """按任务负责 agent 的默认预算估算费用（D-1.8）。specs 为轻量 dict。"""
        total = 0.0
        for s in specs:
            slug = s.get("agent_slug") if isinstance(s, dict) else getattr(s, "agent_slug", "")
            agent = await self.registry.get_agent_by_slug(session, str(slug or ""))
            if agent is None:
                continue
            budget = Budget(**(agent.default_budget or {}))
            total += budget.max_cost if budget.max_cost is not None else 0.5
        return round(total, 4)

    async def _orchestrator_model_config(self, session: AsyncSession) -> ModelConfig | None:
        cfg = await self._model_config_for(session, orchestrator=True)
        return cfg

    async def _model_cfg_for_run(self, run_id: str) -> ModelConfig | None:
        async with db_session() as session:
            return await self._model_config_for(session, orchestrator=True)

    async def _model_config_for(
        self, session: AsyncSession, orchestrator: bool
    ) -> ModelConfig | None:
        from app.runtime.model_config import resolve_model_config

        return await resolve_model_config(session, self.settings, orchestrator=orchestrator)

    async def _build_digest(self, session: AsyncSession, run: Run) -> str:
        tasks = await self._run_tasks(session, run.id)
        agents = await self.registry.list_agents(session)
        by_id = {a.id: a for a in agents}
        return await build_run_digest(session, run, tasks, by_id)

    def _compact_thread(self, thread: list[dict[str, Any]], context_window: int) -> None:
        """超过上下文 60% 时压缩旧轮次（§5.11）。

        用户消息（需求/补充/回答/驳回理由）完整保留（截 500 字，与 digest 一致）：
        这些是模型理解意图的唯一来源，压缩即「失忆」；仅压缩 assistant 与工具结果轮。
        """
        total_chars = sum(len(str(m.get("content"))) for m in thread)
        limit = int(context_window * 3 * 0.6)
        if total_chars <= limit or len(thread) <= 4:
            return
        keep = thread[-4:]
        summary_items: list[str] = []
        user_items: list[str] = []
        for m in thread[:-4]:
            content = m.get("content")
            if m.get("role") == "user" and isinstance(content, str):
                user_items.append(content[:500])
                continue
            if isinstance(content, str):
                summary_items.append(content[:200])
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        summary_items.append(f"调用工具 {block.get('name')}")
        lines = ["【历史压缩】此前唤醒的对话已压缩："]
        if user_items:
            lines.append("用户输入（完整保留）：")
            lines.extend(f"- {u}" for u in user_items)
        lines.extend(f"- {s[:160]}" for s in summary_items[-20:])
        summary = "\n".join(lines)
        new_thread: list[dict[str, Any]] = [{"role": "user", "content": summary}]
        new_thread.extend(keep)
        thread.clear()
        thread.extend(new_thread)

    async def _fail_run(self, run_id: str, error: str) -> None:
        try:
            async with db_session() as session:
                run = await self._load_run(session, run_id)
                if run is None or run.status.is_terminal:
                    return
                await self.events.emit(
                    session, type="run.failed", actor="system", run_id=run_id,
                    payload={"error": error},
                )
                await transition_run(
                    session, self.events, run, RunStatus.failed, reason=error, actor="system"
                )
                run.outcome = RunOutcome.failed
                run.summary_of_gaps = error
        except Exception:
            logger.exception("标记运行失败时出错 run=%s", run_id)

    async def _fail_run_in_session(self, session: AsyncSession, run: Run, error: str) -> None:
        await self.events.emit(
            session, type="run.failed", actor="system", run_id=run.id, payload={"error": error}
        )
        await transition_run(
            session, self.events, run, RunStatus.failed, reason=error, actor="system"
        )
        run.outcome = RunOutcome.failed
        run.summary_of_gaps = error

    async def _kick_scheduler(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.schedule()

    def open_questions(self, run_id: str) -> list[PendingQuestion]:
        return list(self._questions.get(run_id, {}).values())


def assistant_content(
    blocks: list[Any], tool_blocks: list[Any]
) -> list[dict[str, Any]]:
    """组装 assistant 回发 content（wire 格式 block 数组）。

    历史缺陷：生成器产出 `[block]` 列表导致 * 解包后 content 出现
    列表套列表（text+tool_use 混合轮触发，端点 422）。
    本函数是唯一组装入口，形状由 tests/unit/test_model_client_shape.py 焊死。
    """
    return [
        # thinking 必须最先回传（Anthropic：tool_use 轮的 thinking 需原样带回，含 signature）
        *(
            {"type": "thinking", "thinking": b.text, "signature": b.signature}
            for b in blocks
            if b.type == "thinking" and b.text
        ),
        *({"type": "text", "text": b.text} for b in blocks if b.type == "text" and b.text),
        *(
            {"type": "tool_use", "id": b.tool_id, "name": b.tool_name, "input": b.tool_input}
            for b in tool_blocks
        ),
    ]


def _check_acyclic(specs: list[PlanTaskSpec]) -> None:
    _check_acyclic_keys({s.key for s in specs}, {s.key: list(s.depends_on) for s in specs})


def _check_acyclic_keys(keys: set[str], deps: dict[str, list[str]]) -> None:
    for _key, dep_list in deps.items():
        for d in dep_list:
            if d not in keys:
                raise ControlToolError(f"依赖的任务不存在: {d}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ControlToolError(f"计划依赖存在环: {node}")
        visiting.add(node)
        for d in deps.get(node, []):
            visit(d)
        visiting.discard(node)
        visited.add(node)

    for k in keys:
        visit(k)


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


def _safe_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """事件中的参数脱敏：截断超长字段。"""
    safe: dict[str, Any] = {}
    for k, v in arguments.items():
        if isinstance(v, str) and len(v) > 500:
            safe[k] = v[:500] + "…"
        else:
            safe[k] = v
    return safe

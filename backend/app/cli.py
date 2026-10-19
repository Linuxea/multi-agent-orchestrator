"""命令行入口（§14.2 第一期）：platform run "需求"，终端打印事件流。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from app.bootstrap import reset_container
from app.config import get_config
from app.db import dispose_engine, init_db
from app.orchestrator.helpers import db_session


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def _mock_url() -> str:
    """模拟模型地址：PLATFORM_MOCK_MODEL_URL 或 MOCK_MODEL_URL（兼容简写）。"""
    import os

    return os.environ.get("MOCK_MODEL_URL", "") or get_config().mock_model_url


async def _demo_downgrade_executors() -> int:
    """演示模式（模拟模型）：仅在 Claude Code 运行时不可用时把 sdk 执行器临时改为 lite。

    SdkExecutor 已在第二期实现（§14.3）；无运行时环境（无 claude CLI）的演示
    库自动降级以保证可演示性，见 DECISIONS.md D-2.5。仅影响本地演示库。
    """
    from sqlalchemy import select

    from app.enums import ExecutorKind
    from app.models import Agent
    from app.runtime.sdk import sdk_runtime_available

    if sdk_runtime_available():
        return 0

    async with db_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.executor == ExecutorKind.sdk)
        )
        agents = list(result.scalars().all())
        for a in agents:
            a.executor = ExecutorKind.lite
        return len(agents)


async def _cmd_init() -> None:
    await init_db()
    mock_url = _mock_url()
    if mock_url:
        await _ensure_mock_profile(mock_url)
    from app.registry.presets import seed_presets
    from app.tools.service import seed_builtin_tools

    async with db_session() as session:
        builtins = await seed_builtin_tools(session)
        print(f"内置工具：{len(builtins)} 个新建")
        created = await seed_presets(session)
        print(f"预置 agent：{len(created)} 个新建")
    if mock_url:
        n = await _demo_downgrade_executors()
        if n:
            print(f"演示模式：{n} 个 sdk 执行器已临时降级为 lite（未检测到 Claude Code 运行时）")
    print("数据库初始化完成。")


async def _approve_once(container: Any, run_id: str) -> bool:
    """若运行在等计划确认则批准最新版本；返回是否执行了批准。"""
    from sqlalchemy import select

    from app.models import Plan, Run

    async with db_session() as session:
        run = await session.get(Run, run_id)
        if run is None or run.status.value != "awaiting_plan_approval":
            return False
        result = await session.execute(
            select(Plan)
            .where(Plan.run_id == run_id, Plan.approved.is_(False))
            .order_by(Plan.version.desc())
            .limit(1)
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            return False
    await container.engine.approve_plan(run_id, plan.version)
    return True


async def _cmd_run(request: str, auto_approve: bool = False) -> int:
    mock_url = _mock_url()
    if mock_url:
        await _ensure_mock_profile(mock_url)
        await _demo_downgrade_executors()

    container = reset_container()
    queue = await container.bus.subscribe(["global"])

    async def _printer() -> None:
        while True:
            msg = await queue.get()
            prefix = f"[{msg.ts[11:19]}]"
            where = f" {msg.task_key}" if msg.task_key else ""
            line = _format_event(msg)
            print(f"{prefix} {msg.event_type}{where}: {line}")

    printer = asyncio.create_task(_printer())
    try:
        async with db_session() as session:
            from app.ids import new_id
            from app.models import Session

            s = Session(id=new_id("sess"), title=request[:30])
            session.add(s)
            await session.flush()
            session_id = s.id

        _, run = await container.engine.submit_user_message(session_id, request)
        print(f"运行已创建：{run.id}")
        ok = await container.engine.quiesce(run.id, timeout=600)
        if auto_approve and ok:
            # 静止后若在等计划确认：批准并继续（演示模式）
            for _ in range(10):
                if not await _approve_once(container, run.id):
                    break
                ok = await container.engine.quiesce(run.id, timeout=600)
                if not ok:
                    break
        if not ok:
            print("（等待超时，运行可能仍在进行或等待用户输入）")

        async with db_session() as session:
            from app.models import Run

            final = await session.get(Run, run.id)
            if final is not None:
                print("\n" + "=" * 60)
                if final.final_answer:
                    print("最终答复：\n")
                    print(final.final_answer)
                else:
                    print(f"运行状态：{final.status.value}")
                if final.summary_of_gaps:
                    print(f"\n未完成与限制：\n{final.summary_of_gaps}")
                usage = final.usage or {}
                print(f"\n用量：费用 {usage.get('cost', 0):.4f}，"
                      f"输入 {usage.get('input_tokens', 0)} tok，"
                      f"输出 {usage.get('output_tokens', 0)} tok")
        return 0
    finally:
        printer.cancel()
        await container.bus.unsubscribe(queue, ["global"])
        await dispose_engine()


def _format_event(msg: Any) -> str:
    p = msg.payload or {}
    t = msg.event_type
    if t == "task.status_changed":
        return f"{p.get('key')} {p.get('from_status')} → {p.get('to_status')} {p.get('reason', '')}"
    if t == "task.created":
        return f"{p.get('key')}「{p.get('title')}」@{p.get('agent_slug')}"
    if t == "attempt.started":
        return f"第 {p.get('number')} 次尝试开始（{p.get('agent_slug')}）"
    if t == "attempt.reported":
        report = p.get("report") or {}
        return f"status={report.get('status')} {str(report.get('summary', ''))[:60]}"
    if t == "decision.recorded":
        return f"{p.get('type')}：{p.get('summary')}"
    if t == "orchestrator.message":
        return str(p.get("text", ""))[:80]
    if t == "orchestrator.tool_rejected":
        return f"{p.get('tool')} 被拒绝：{p.get('reason')}"
    if t == "plan.proposed":
        return f"v{p.get('version')}（{p.get('task_count')} 任务，预估 {p.get('estimated_cost')}）"
    if t == "run.status_changed":
        return f"{p.get('from_status')} → {p.get('to_status')}"
    if t == "artifact.created":
        return f"{p.get('name')}（{p.get('task_key') or '用户上传'}）"
    if t in ("attempt.output", "attempt.thinking"):
        return str(p.get("text", ""))[:100]
    if t == "budget.warning":
        return f"预算告警 {p.get('level')}"
    return json.dumps(p, ensure_ascii=False)[:100]


async def _ensure_mock_profile(mock_url: str) -> None:
    """配置了 MOCK_MODEL_URL 时确保存在指向模拟服务的默认模型配置。"""
    from sqlalchemy import select

    from app.models import ModelProfile

    async with db_session() as session:
        result = await session.execute(select(ModelProfile).limit(1))
        if result.scalar_one_or_none() is not None:
            return
        from app.ids import new_id

        profile = ModelProfile(
            id=new_id("mp"),
            name="模拟模型",
            base_url=mock_url,
            model="mock-model",
            max_output_tokens=4096,
            is_default_orchestrator=True,
            is_default_worker=True,
        )
        session.add(profile)
        await session.flush()


def main() -> None:
    parser = argparse.ArgumentParser(prog="platform", description="多 Agent 协调平台 CLI")
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="初始化数据库并创建预置 agent")
    run_parser = sub.add_parser("run", help="运行一次需求")
    run_parser.add_argument("request", help="用户需求文本")
    run_parser.add_argument(
        "--auto-approve", action="store_true", help="自动确认计划（演示用）"
    )
    args = parser.parse_args()

    _setup_logging(args.verbose)
    if args.cmd == "init":
        asyncio.run(_cmd_init())
    elif args.cmd == "run":
        sys.exit(asyncio.run(_cmd_run(args.request, args.auto_approve)))


if __name__ == "__main__":
    main()

"""真实端点编排 e2e（永久回归资产）。

背景：本地 mock 不校验请求形状/协议细节，多轮 thread 的真实缺陷
（422 反序列化、thinking 回传、schema $defs 等）只有真实端点能暴露。
本文件用真实 DeepSeek 跑完整协调循环，作为发布前的最终防线。

运行：
    DEEPSEEK_API_KEY=sk-... uv run pytest tests/e2e/test_real_orchestration.py -m real_model
可选环境变量：DEEPSEEK_MODEL（默认 deepseek-flash）、DEEPSEEK_BASE_URL。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from sqlalchemy import select

from app.enums import RunOutcome, RunStatus
from app.events.service import load_events
from app.ids import new_id
from app.models import ModelProfile, Run, Secret, Session
from app.orchestrator.helpers import db_session
from app.runtime.types import ModelConfig

pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"), reason="未设置 DEEPSEEK_API_KEY，跳过真实编排 e2e"
    ),
]

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

# 应答引导：明确要求走 search_agents → 汇总 → finalize 链路（覆盖多轮工具循环），
# 且不派发子任务（免执行、省钱、快）
GUIDE_ANSWER = (
    "请查看平台上有哪些可用的 agent（用工具搜索），"
    "然后用两三句话汇总介绍给我，最后提交最终答复并结束。不需要派发任何任务。"
)


@pytest.fixture
async def real_profile(env: Any) -> str:
    """在隔离环境里建立指向真实端点的模型配置（密钥走密钥库加密）。"""
    from app import secrets as secrets_util

    async with db_session() as s:
        sec = Secret(
            id=new_id("sec"),
            name=f"MODEL_KEY::{MODEL}-real-e2e",
            encrypted_value=secrets_util.encrypt(os.environ["DEEPSEEK_API_KEY"]),
            description="真实编排 e2e",
        )
        s.add(sec)
        await s.flush()
        profile = ModelProfile(
            id=new_id("mp"),
            name="ds-real-e2e",
            protocol="anthropic_messages",
            base_url=BASE_URL,
            model=MODEL,
            max_output_tokens=4096,
            context_window=128000,
            is_default_orchestrator=True,
            is_default_worker=True,
            api_key_secret_id=sec.id,
        )
        s.add(profile)
        sess = Session(id="sess_real_e2e", title="真实编排")
        s.add(sess)
        await s.commit()
        from app.bootstrap import get_container

        container = get_container()
        async with db_session() as s2:
            await container.settings.patch(
                s2, {"orchestrator_model_profile_id": profile.id}
            )
        return profile.id


def _engine_thread() -> list[dict[str, Any]]:
    """engine._run_model_loop 组装的多轮 thread 形状样本（含工具循环）。"""
    import json

    return [
        {"role": "user", "content": "【系统唤醒】触发原因：new_user_message\n\n当前运行状态摘要：\n（摘要）"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "先了解可用人手。"},
                {
                    "type": "tool_use",
                    "id": "toolu_e2e_01",
                    "name": "search_agents",
                    "input": {"tags": [], "rationale": "盘点可用 agent"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_e2e_01",
                    "content": json.dumps({"agents": []}, ensure_ascii=False),
                }
            ],
        },
        {"role": "assistant", "content": "平台内暂无可用 agent。"},
        {"role": "user", "content": "【系统唤醒】触发原因：user_answered\n\n【用户回答】答：继续汇总"},
    ]


async def test_engine_thread_shape_against_real_endpoint(real_profile: str) -> None:
    """engine 形状的多轮 thread 直接对真实端点往返一次（形状兼容性快速回归）。"""
    from app.runtime.model_client import ModelCaller

    cfg = ModelConfig(
        profile_id=real_profile,
        model=MODEL,
        base_url=BASE_URL,
        api_key=os.environ["DEEPSEEK_API_KEY"],
        max_output_tokens=128,
    )
    caller = ModelCaller(cfg)
    resp = await caller.create(
        system="你是测试助手。",
        messages=_engine_thread(),
        max_tokens=64,
    )
    assert resp.blocks, "真实端点应返回内容"
    assert resp.usage.input_tokens > 0


async def test_orchestrator_full_cycle_real_model(real_profile: str) -> None:
    """完整协调循环：模糊输入 → ask_user 澄清 → 应答 → 工具循环 → finalize 终态。

    覆盖：多轮 thread 真实往返（422/形状缺陷防线）、用户输入可见性
    （回答必须被模型理解并驱动后续动作）、finalize 收尾。
    """
    from app.bootstrap import get_container

    engine = get_container().engine
    _msg, run = await engine.submit_user_message("sess_real_e2e", "你好")

    answered = 0
    terminal = False
    for _ in range(400):  # 最多 ~120s
        await asyncio.sleep(0.3)
        async with db_session() as s:
            r = (await s.execute(select(Run).where(Run.id == run.id))).scalar_one()
            if r.status.is_terminal:
                terminal = True
                break
        for q in engine.open_questions(run.id):
            answered += 1
            if answered > 3:
                pytest.fail("模型连续澄清超过 3 轮仍未推进——用户回答未被有效理解")
            await engine.submit_answer(run.id, GUIDE_ANSWER, question_id=q.question_id)
            break

    assert terminal or await engine.quiesce(run.id, timeout=240), "运行应在限时内静止"

    async with db_session() as s:
        r = (await s.execute(select(Run).where(Run.id == run.id))).scalar_one()
        events = await load_events(s, run_id=run.id)
    failures = [e for e in events if e.type in ("run.failed",)]
    assert not failures, f"不应出现 run.failed：{[e.payload for e in failures][:3]}"
    # 诊断输出：模型最后的动作（-s 时可见）
    print(f"\n[diag] run status={r.status.value}, answered={answered}, events={len(events)}")
    for e in events[-14:]:
        text = str(e.payload.get("text") or e.payload.get("error") or e.payload)[:150]
        print(f"[diag] {e.seq:>4} {e.type:<26} {text}")
    assert r.status == RunStatus.completed, f"终态应为 completed，实际 {r.status.value}"
    assert (r.final_answer or "").strip(), "finalize 后应有最终答复"
    assert r.outcome == RunOutcome.success, f"零任务直接答复应为 success，实际 {r.outcome}"

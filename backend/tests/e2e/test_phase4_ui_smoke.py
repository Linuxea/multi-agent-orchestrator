"""第四期界面链路冒烟（真实进程）：Agent 工厂 + 团队编队。

启动 uvicorn（含 dist 静态服务）+ 模拟模型，走 UI 依赖的完整 API 面：
工厂起草 agent → origin=factory 保存 / 工厂编队 → 团队 + 剧本落库 /
会话绑定编队 → 计划阶段映射与偏离记录 / 任务 playbook_stage_key。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import httpx  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]  # platform/
BACKEND = ROOT / "backend"
WORK = Path("/tmp/opencode/phase4-smoke")
WORK.mkdir(parents=True, exist_ok=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_http(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1.5)
            if r.status_code < 500:
                return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"服务未就绪: {url}")


def main() -> int:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("✗ 未设置 DEEPSEEK_API_KEY：真实模型冒烟无法运行（本冒烟不使用模拟模型）")
        return 2
    api_port = free_port()
    db = WORK / "smoke.db"
    if db.exists():
        db.unlink()

    env = {
        **os.environ,
        "PLATFORM_DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "PLATFORM_DATA_DIR": str(WORK / "data"),
        "PLATFORM_MASTER_KEY": "z0GFc6hvEB6iFpRlK3YD8M_x1V2wQ4t7S6uJ9nB0kCw=",
    }
    procs: list[subprocess.Popen] = []
    try:
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(api_port)],
                cwd=BACKEND,
                env=env,
            )
        )
        wait_http(f"http://127.0.0.1:{api_port}/healthz")
        base = f"http://127.0.0.1:{api_port}"

        checks: list[tuple[str, bool, str]] = []

        # 1. 前端静态产物由后端服务（与第三期共用 dist）
        r = httpx.get(base + "/")
        checks.append(("GET / 服务前端 index.html", r.status_code == 200 and "root" in r.text, f"status={r.status_code}"))

        # 1.5 真实模型配置就绪（密钥走密钥库）
        r = httpx.post(
            base + "/api/v1/model-profiles",
            json={
                "name": "ds-real-smoke4",
                "protocol": "anthropic_messages",
                "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic"),
                "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
                "max_output_tokens": 8192,
                "context_window": 128000,
                "is_default_orchestrator": True,
                "is_default_worker": True,
                "api_key": os.environ["DEEPSEEK_API_KEY"],
            },
            timeout=30,
        )
        assert r.status_code in (200, 201), r.text

        # 2. 工厂起草 agent → 草稿字段齐全（真实模型：结构 + 语义断言）
        r = httpx.post(base + "/api/v1/factory/agents",
                       json={"requirement": "需要一个市场侦察 agent，负责收集竞品动态", "executor": "lite"},
                       timeout=120)
        draft = r.json() if r.status_code == 200 else {}
        blob = (draft.get("system_prompt") or "") + (draft.get("description") or "") + (draft.get("role_detail") or "")
        checks.append(("POST /factory/agents 起草",
                       r.status_code == 200 and bool(draft.get("slug")) and bool(draft.get("system_prompt"))
                       and any(k in blob for k in ("市场", "竞品", "侦察", "market")),
                       f"status={r.status_code}"))
        model_id = httpx.get(base + "/api/v1/model-profiles").json()[0]["id"]

        # 3. 草稿保存 → origin=factory
        r = httpx.post(base + "/api/v1/agents", json={
            "slug": draft["slug"], "name": draft["name"],
            "description": draft["description"], "role_detail": draft["role_detail"],
            "capability_tags": draft["capability_tags"],
            "system_prompt": draft["system_prompt"],
            "executor": "lite", "model_profile_id": model_id,
            "default_budget": draft["default_budget"],
            "status": "draft", "origin": "factory",
            "input_contract": draft["input_contract"], "output_contract": draft["output_contract"],
            "avatar": draft["avatar"],
        })
        saved = r.json() if r.status_code == 201 else {}
        checks.append(("POST /agents origin=factory",
                       saved.get("origin") == "factory",
                       f"status={r.status_code}"))
        r = httpx.post(base + "/api/v1/agents", json={
            "slug": "x", "name": "x", "description": "x", "system_prompt": "x",
            "model_profile_id": model_id, "origin": "preset",
        })
        checks.append(("origin=preset 被拒", r.status_code == 400, f"status={r.status_code}"))

        # 4. 工厂编队 → 成员过滤 + 剧本阶段（真实模型：结构断言）
        r = httpx.post(base + "/api/v1/factory/teams",
                       json={"requirement": "组一个写简报的团队：先调研再成文"}, timeout=120)
        tdraft = r.json() if r.status_code == 200 else {}
        stages = tdraft.get("playbook", {}).get("stages", [])
        stage_keys = [s.get("key") for s in stages if isinstance(s, dict) and s.get("key")]
        checks.append(("POST /factory/teams 编队",
                       r.status_code == 200 and bool(tdraft.get("slug"))
                       and len(tdraft.get("member_agent_ids", [])) >= 1
                       and len(stage_keys) >= 1,
                       f"status={r.status_code}"))

        # 5. 团队 + 剧本落库
        r = httpx.post(base + "/api/v1/teams", json={
            "slug": tdraft["slug"], "name": tdraft["name"],
            "description": tdraft["description"],
            "member_agent_ids": tdraft["member_agent_ids"],
            "lead_hint": tdraft["lead_hint"],
        })
        team = r.json() if r.status_code == 201 else {}
        checks.append(("POST /teams", bool(team.get("id")), f"status={r.status_code}"))
        r = httpx.put(f"{base}/api/v1/teams/{team['id']}/playbook", json={
            "name": tdraft["playbook"]["name"],
            "trigger_hint": tdraft["lead_hint"],
            "stages": stages,
        })
        checks.append(("PUT /teams/{id}/playbook",
                       r.status_code == 201 and len(r.json()["stages"]) == len(stages), f"status={r.status_code}"))
        r = httpx.put(f"{base}/api/v1/teams/{team['id']}/playbook", json={
            "name": "坏", "trigger_hint": "", "stages": [{"name": "缺 key"}],
        })
        checks.append(("剧本阶段缺 key → 400", r.status_code == 400, f"status={r.status_code}"))

        # 6. 会话绑定编队（PATCH）+ 校验
        r = httpx.post(base + "/api/v1/sessions", json={"title": "编队冒烟"})
        session_id = r.json()["id"]
        r = httpx.patch(f"{base}/api/v1/sessions/{session_id}",
                        json={"default_team_id": team["id"]})
        checks.append(("PATCH /sessions 绑定编队",
                       r.status_code == 200 and r.json()["default_team_id"] == team["id"],
                       f"status={r.status_code}"))
        r = httpx.patch(f"{base}/api/v1/sessions/{session_id}",
                        json={"default_team_id": "team_none"})
        checks.append(("绑定不存在团队 → 404", r.status_code == 404, f"status={r.status_code}"))

        # 7. 发消息 → 计划（阶段映射）→ 批准 → 终态
        r = httpx.post(f"{base}/api/v1/sessions/{session_id}/messages",
                       json={"content": "写一份两句话的市场简报（凭已有知识完成，不需要联网或使用任何外部工具）"},
                       timeout=30)
        assert r.status_code == 201, r.text
        run_id = r.json()["run_id"]

        last_answered_q = ""
        deadline = time.monotonic() + 600
        outcome = None
        approved_at_waiting = False
        answered = 0
        run: dict[str, Any] = {}
        while time.monotonic() < deadline:
            run = httpx.get(f"{base}/api/v1/runs/{run_id}").json()
            if run["status"] == "awaiting_plan_approval" and not approved_at_waiting:
                approved_at_waiting = True
                r = httpx.post(
                    f"{base}/api/v1/runs/{run_id}/plans/{run['current_plan_version']}/approve",
                    json={},
                )
                assert r.status_code == 204, r.text
            if run["status"] in ("completed", "failed", "cancelled"):
                outcome = run["outcome"]
                break
            # 澄清提问自动应答（run 级 clarifying 与任务级提问均覆盖，最多 3 轮）
            q = httpx.get(f"{base}/api/v1/runs/{run_id}/events",
                          params={"after_seq": 0, "types": "user.question_asked"}).json()["items"]
            latest_q = q[-1]["payload"].get("question_id") if q else None
            need_answer = run["status"] == "clarifying" or (latest_q and latest_q != last_answered_q)
            if need_answer and answered < 3:
                answered += 1
                last_answered_q = latest_q or f"clarify-{answered}"
                httpx.post(f"{base}/api/v1/runs/{run_id}/answers",
                           json={"answer": "跳过无法完成的任务，用已有信息完成其余部分并终答",
                                 "question_id": None}, timeout=15)
            time.sleep(1.5)
        checks.append(("编队运行待批 → 计划卡批准", approved_at_waiting, ""))
        checks.append(("编队运行到达终态", outcome in ("success", "partial"), f"status={run.get('status')} outcome={outcome}"))

        # 8. 阶段落库 + 偏离记录（真实模型：任务阶段引用必须合法）
        r = httpx.get(f"{base}/api/v1/runs/{run_id}/tasks")
        stage_by_key = {t["key"]: t["playbook_stage_key"] for t in r.json()}
        valid_stages = set(stage_keys) | {""}
        checks.append(("任务 playbook_stage_key 合法落库",
                       bool(stage_by_key) and all(s in valid_stages for s in stage_by_key.values()),
                       f"{stage_by_key} ⊂ {stage_keys}"))
        events = httpx.get(f"{base}/api/v1/runs/{run_id}/events?after_seq=0").json()["items"]
        proposed = [e for e in events if e["type"] == "plan.proposed"]
        checks.append(("plan.proposed 记录偏离字段",
                       proposed and isinstance(proposed[-1]["payload"].get("deviates_from_playbook"), list),
                       f"{[p['payload'].get('deviates_from_playbook') for p in proposed]}"))
        plans = httpx.get(f"{base}/api/v1/runs/{run_id}/plans").json()
        snap_stages = {t.get("playbook_stage_key") or "" for t in plans[-1]["tasks_snapshot"]}
        checks.append(("计划快照含阶段映射",
                       bool(snap_stages - {""}) and snap_stages <= set(stage_keys) | {""},
                       f"{snap_stages} ⊂ {stage_keys}"))

        # 汇总
        print("\n== 第四期界面链路冒烟 ==")
        failed = 0
        for name, ok, detail in checks:
            mark = "✓" if ok else "✗"
            if not ok:
                failed += 1
            print(f" {mark} {name}  {detail}")
        print(f"\n{len(checks) - failed}/{len(checks)} 通过")
        return 1 if failed else 0
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(main())

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
    model_port, api_port = free_port(), free_port()
    db = WORK / "smoke.db"
    if db.exists():
        db.unlink()

    env = {
        **os.environ,
        "PLATFORM_DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "PLATFORM_DATA_DIR": str(WORK / "data"),
        "PLATFORM_MASTER_KEY": "z0GFc6hvEB6iFpRlK3YD8M_x1V2wQ4t7S6uJ9nB0kCw=",
        "MOCK_MODEL_URL": f"http://127.0.0.1:{model_port}",
    }
    procs: list[subprocess.Popen] = []
    try:
        procs.append(
            subprocess.Popen(
                [
                    sys.executable, "-m", "tests.mockmodel.server_main",
                    "--port", str(model_port),
                    "--script", "tests/scripts/phase4_smoke.yaml",
                ],
                cwd=BACKEND,
            )
        )
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(api_port)],
                cwd=BACKEND,
                env=env,
            )
        )
        wait_http(f"http://127.0.0.1:{model_port}/health")
        wait_http(f"http://127.0.0.1:{api_port}/healthz")
        base = f"http://127.0.0.1:{api_port}"

        checks: list[tuple[str, bool, str]] = []

        # 1. 前端静态产物由后端服务（与第三期共用 dist）
        r = httpx.get(base + "/")
        checks.append(("GET / 服务前端 index.html", r.status_code == 200 and "root" in r.text, f"status={r.status_code}"))

        # 2. 工厂起草 agent → 草稿字段齐全
        r = httpx.post(base + "/api/v1/factory/agents",
                       json={"requirement": "需要一个市场侦察 agent", "executor": "lite"}, timeout=60)
        draft = r.json() if r.status_code == 200 else {}
        checks.append(("POST /factory/agents 起草",
                       r.status_code == 200 and draft.get("slug") == "market-scout"
                       and bool(draft.get("system_prompt"))
                       and draft.get("slug_available") is True,
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
                       saved.get("origin") == "factory" and saved.get("input_contract") == "一句需求",
                       f"status={r.status_code}"))
        r = httpx.post(base + "/api/v1/agents", json={
            "slug": "x", "name": "x", "description": "x", "system_prompt": "x",
            "model_profile_id": model_id, "origin": "preset",
        })
        checks.append(("origin=preset 被拒", r.status_code == 400, f"status={r.status_code}"))

        # 4. 工厂编队 → 成员过滤 + 剧本阶段
        r = httpx.post(base + "/api/v1/factory/teams",
                       json={"requirement": "组一个写简报的团队"}, timeout=60)
        tdraft = r.json() if r.status_code == 200 else {}
        stages = tdraft.get("playbook", {}).get("stages", [])
        checks.append(("POST /factory/teams 编队",
                       r.status_code == 200 and tdraft.get("slug") == "brief-squad"
                       and len(tdraft.get("member_agent_ids", [])) == 2
                       and [s.get("key") for s in stages] == ["S1", "S2", "S3"],
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
                       r.status_code == 201 and len(r.json()["stages"]) == 3, f"status={r.status_code}"))
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
                       json={"content": "写一份市场简报"}, timeout=30)
        assert r.status_code == 201, r.text
        run_id = r.json()["run_id"]

        deadline = time.monotonic() + 180
        outcome = None
        approved_at_waiting = False
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
            time.sleep(1.0)
        checks.append(("编队运行待批 → 计划卡批准", approved_at_waiting, ""))
        checks.append(("编队运行到达终态", outcome == "success", f"status={run.get('status')} outcome={outcome}"))

        # 8. 阶段落库 + 偏离记录
        r = httpx.get(f"{base}/api/v1/runs/{run_id}/tasks")
        stage_by_key = {t["key"]: t["playbook_stage_key"] for t in r.json()}
        checks.append(("任务 playbook_stage_key 落库",
                       stage_by_key == {"T1": "S1", "T2": "S2"}, f"{stage_by_key}"))
        events = httpx.get(f"{base}/api/v1/runs/{run_id}/events?after_seq=0").json()["items"]
        proposed = [e for e in events if e["type"] == "plan.proposed"]
        checks.append(("plan.proposed 记录偏离（S3 无任务映射）",
                       proposed and proposed[-1]["payload"].get("deviates_from_playbook") == ["S3"],
                       f"{[p['payload'].get('deviates_from_playbook') for p in proposed]}"))
        plans = httpx.get(f"{base}/api/v1/runs/{run_id}/plans").json()
        snap_stages = {t.get("playbook_stage_key") for t in plans[-1]["tasks_snapshot"]}
        checks.append(("计划快照含阶段映射", snap_stages == {"S1", "S2"}, f"{snap_stages}"))

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

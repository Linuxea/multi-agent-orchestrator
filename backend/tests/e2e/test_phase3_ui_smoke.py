"""第三期界面链路冒烟（真实进程）：

启动 uvicorn（含 dist 静态服务）+ 模拟模型，走 UI 依赖的完整 API 面：
静态首页 / 认证开关 / 会话消息→运行 / 事件补发 / WS 订阅推送 / 交付物清单。
"""

from __future__ import annotations

import asyncio
import json
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
WORK = Path("/tmp/opencode/phase3-smoke")
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


async def ws_smoke(ws_port: int, run_id: str, expect_types: set[str]) -> list[dict[str, Any]]:
    """原生 WS：订阅 run 通道，收齐期望事件类型后返回。"""
    import websockets  # type: ignore[import-not-found]

    uri = f"ws://127.0.0.1:{ws_port}/api/v1/ws"
    got: list[dict[str, Any]] = []
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"op": "subscribe", "channel": f"run:{run_id}", "after_seq": 0}))
        # 补发历史
        for _ in range(500):
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("op") == "subscribed":
                break
            got.append(msg)
    # 补发应包含运行全部持久化事件
    return got


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
                    "--script", "tests/scripts/multi_domain_research.yaml",
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

        # 1. 前端静态产物由后端服务
        r = httpx.get(base + "/")
        checks.append(("GET / 服务前端 index.html", r.status_code == 200 and "root" in r.text and "多 Agent 协调平台" in r.text, f"status={r.status_code}"))

        # 2. 初始化（预置 agent + mock 模型）
        r = httpx.post(base + "/api/v1/sessions", json={"title": "界面冒烟会话"})
        assert r.status_code == 201, r.text
        session_id = r.json()["id"]

        r = httpx.get(base + "/api/v1/agents")
        checks.append(("GET /agents 预置 agent", r.status_code == 200 and len(r.json()) >= 11, f"n={len(r.json())}"))
        r = httpx.get(base + "/api/v1/model-profiles")
        checks.append(("GET /model-profiles 模拟模型", r.status_code == 200 and len(r.json()) >= 1, ""))
        r = httpx.get(base + "/api/v1/tools")
        checks.append(("GET /tools 内置工具", r.status_code == 200 and len(r.json()) >= 5, f"n={len(r.json())}"))
        r = httpx.get(base + "/api/v1/settings")
        checks.append(("GET /settings", r.status_code == 200 and "plan_approval_mode" in r.json(), ""))

        # 3. 发消息 → 创建运行 → 待批计划时模拟 UI 计划卡「通过」→ 跑到终态
        r = httpx.post(f"{base}/api/v1/sessions/{session_id}/messages",
                       json={"content": "调研 2026 年 AI 编程工具市场并写一份简报"}, timeout=30)
        assert r.status_code == 201, r.text
        run_id = r.json()["run_id"]

        deadline = time.monotonic() + 180
        outcome = None
        approved_at_waiting = False
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
        checks.append(("待批计划 → UI 计划卡批准生效", approved_at_waiting, ""))
        checks.append(("运行到达终态", outcome in ("success", "partial", "failed"), f"status={run['status']} outcome={outcome}"))

        r = httpx.get(f"{base}/api/v1/runs/{run_id}")
        run = r.json()
        checks.append(("运行详情含任务/预算/答复", bool(run["tasks"]) and run["final_answer"] is not None, f"tasks={len(run['tasks'])}"))

        r = httpx.get(f"{base}/api/v1/runs/{run_id}/events?after_seq=0")
        events = r.json()["items"]
        types = {e["type"] for e in events}
        checks.append(("事件补发（含计划/派发/复核/终局）",
                       {"plan.proposed", "attempt.started", "run.completed"} <= types, f"n={len(events)}"))

        r = httpx.get(f"{base}/api/v1/runs/{run_id}/artifacts")
        checks.append(("GET /runs/{id}/artifacts", r.status_code == 200, f"n={len(r.json())}"))

        r = httpx.get(f"{base}/api/v1/runs/{run_id}/decisions")
        checks.append(("GET /runs/{id}/decisions", r.status_code == 200 and len(r.json()) > 0, ""))

        r = httpx.get(f"{base}/api/v1/runs/{run_id}/plans")
        checks.append(("GET /runs/{id}/plans", r.status_code == 200 and len(r.json()) >= 1, ""))

        # 4. WS 订阅 + 补发
        got = asyncio.run(ws_smoke(api_port, run_id, set()))
        checks.append(("WS 订阅补发事件", len(got) >= len(events), f"ws={len(got)} http={len(events)}"))

        # 5. 访问密码启用 → 401 → 携带密钥 200
        r = httpx.patch(base + "/api/v1/settings", json={"changes": {"access_password": "smoke-key"}})
        assert r.status_code == 200, r.text
        time.sleep(5.5)  # 等设置缓存过期
        r = httpx.get(base + "/api/v1/sessions")
        checks.append(("启用密码后未携带密钥 → 401", r.status_code == 401, f"status={r.status_code}"))
        r = httpx.get(f"{base}/api/v1/sessions", headers={"X-Platform-Key": "smoke-key"})
        checks.append(("携带密钥 → 200", r.status_code == 200, ""))
        r = httpx.get(base + "/api/v1/auth/status")
        checks.append(("auth/status 始终开放", r.status_code == 200 and r.json()["required"] is True, ""))

        # 汇总
        print("\n== 第三期界面链路冒烟 ==")
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

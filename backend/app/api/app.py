"""FastAPI 应用工厂与公共依赖。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    agents,
    answers,
    approvals,
    artifacts,
    attempts,
    events,
    model_profiles,
    plans,
    runs,
    secrets,
    sessions,
    settings,
    tasks,
    teams,
    tools,
    ws,
)
from app.bootstrap import get_container
from app.db import init_db


def create_app() -> FastAPI:
    app = FastAPI(
        title="多 Agent 协调平台",
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    routers = [
        sessions.router,
        runs.router,
        plans.router,
        tasks.router,
        attempts.router,
        answers.router,
        approvals.router,
        events.router,
        artifacts.router,
        agents.router,
        teams.router,
        tools.router,
        model_profiles.router,
        secrets.router,
        settings.router,
        ws.router,
    ]
    for r in routers:
        app.include_router(r, prefix="/api/v1")

    @app.on_event("startup")
    async def _startup() -> None:
        await init_db()
        get_container()

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["meta"])
    async def readyz() -> dict[str, str]:
        from app.db import check_db_ready

        await check_db_ready()
        return {"status": "ready"}

    @app.get("/metrics", tags=["meta"])
    async def metrics() -> str:
        """Prometheus 文本格式（§13 可观测性，第一期为基础版）。"""
        container = get_container()
        running = len([t for t, _ in container.scheduler._running.values() if not t.done()])  # noqa: SLF001
        lines = [
            "# TYPE platform_running_attempts gauge",
            f"platform_running_attempts {running}",
            "# TYPE platform_event_bus_subscribers gauge",
            f"platform_event_bus_subscribers {len(container.bus._listeners)}",  # noqa: SLF001
        ]
        return "\n".join(lines) + "\n"

    return app

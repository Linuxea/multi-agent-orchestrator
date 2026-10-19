"""交付物（§12.2）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from app.bootstrap import get_container
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["artifacts"])


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str) -> dict[str, object]:
    async with db_session() as session:
        art = await get_container().artifacts.get(session, artifact_id)
        if art is None:
            raise HTTPException(404, "交付物不存在")
        return {
            "id": art.id,
            "run_id": art.run_id,
            "task_id": art.task_id,
            "attempt_id": art.attempt_id,
            "name": art.name,
            "kind": art.kind.value,
            "mime": art.mime,
            "size": art.size,
            "sha256": art.sha256,
            "summary": art.summary,
            "status": art.status.value,
            "derived_from": art.derived_from,
        }


@router.get("/runs/{run_id}/artifacts")
async def list_run_artifacts(run_id: str) -> list[dict[str, object]]:
    """运行维度交付物清单（第三期界面）。"""
    from app.models import Artifact

    async with db_session() as session:
        result = await session.execute(
            select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
        )
        return [
            {
                "id": a.id,
                "run_id": a.run_id,
                "task_id": a.task_id,
                "attempt_id": a.attempt_id,
                "name": a.name,
                "kind": a.kind.value,
                "mime": a.mime,
                "size": a.size,
                "sha256": a.sha256,
                "summary": a.summary,
                "status": a.status.value,
                "derived_from": a.derived_from,
            }
            for a in result.scalars().all()
        ]


@router.get("/artifacts/{artifact_id}/content")
async def artifact_content(artifact_id: str) -> Response:
    container = get_container()
    async with db_session() as session:
        art = await container.artifacts.get(session, artifact_id)
        if art is None:
            raise HTTPException(404, "交付物不存在")
        data = container.artifacts.read_bytes(art)
    return Response(
        content=data,
        media_type=art.mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{art.name}"'},
    )


@router.post("/runs/{run_id}/artifacts", status_code=201)
async def upload_artifact(run_id: str, file: UploadFile) -> dict[str, object]:
    """用户上传输入交付物（task_id 为空，§5.8）。"""
    container = get_container()
    data = await file.read()
    async with db_session() as session:
        art = await container.artifacts.save_bytes(
            session,
            run_id=run_id,
            data=data,
            name=file.filename or "upload.bin",
            summary=f"用户上传：{file.filename or 'upload.bin'}",
            actor="user",
        )
    return {"id": art.id, "name": art.name}


class ArtifactCreate(BaseModel):
    name: str
    content: str
    summary: str = ""


@router.post("/runs/{run_id}/artifacts/json", status_code=201)
async def create_artifact_json(run_id: str, body: ArtifactCreate) -> dict[str, object]:
    container = get_container()
    async with db_session() as session:
        art = await container.artifacts.save_bytes(
            session,
            run_id=run_id,
            data=body.content.encode("utf-8"),
            name=body.name,
            summary=body.summary,
            actor="user",
        )
    return {"id": art.id, "name": art.name}

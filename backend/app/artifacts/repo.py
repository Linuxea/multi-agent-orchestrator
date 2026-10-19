"""交付物仓库（§5.8）：文件不可变存储 + 元数据登记，按引用传递。"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.enums import ArtifactKind, ArtifactStatus
from app.events.service import EventService
from app.ids import new_id
from app.models import Artifact
from app.utils import estimate_tokens

MAX_FULL_READ_TOKENS = 30000  # read_artifact full 模式截断阈值（§5.2）


class ArtifactRepository:
    def __init__(self, event_service: EventService) -> None:
        self.events = event_service

    def _base_dir(self) -> Path:
        base = get_config().data_dir / "artifacts"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _storage_dir(self, run_id: str, task_key: str | None, attempt_no: int | None) -> Path:
        parts = [run_id, task_key or "_user", str(attempt_no or 0)]
        d = self._base_dir().joinpath(*parts)  # noqa: ASYNC240
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def save_bytes(  # noqa: ASYNC240
        self,
        session: AsyncSession,
        *,
        run_id: str,
        data: bytes,
        name: str,
        task_id: str | None = None,
        task_key: str | None = None,
        attempt_id: str | None = None,
        attempt_no: int | None = None,
        kind: str = ArtifactKind.file.value,
        summary: str = "",
        derived_from: list[str] | None = None,
        status: str = ArtifactStatus.submitted.value,
        actor: str = "system",
    ) -> Artifact:
        """原子写入（临时文件 + rename）并登记元数据（§13 可靠性）。"""
        d = self._storage_dir(run_id, task_key, attempt_no)
        final_path = d / name
        if final_path.exists():
            # 文件不可变：同名修订产生新版本名
            stem, suffix = os.path.splitext(name)
            i = 2
            while (d / f"{stem}.{i}{suffix}").exists():
                i += 1
            final_path = d / f"{stem}.{i}{suffix}"

        fd, tmp_name = tempfile.mkstemp(dir=d, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_name, final_path)  # noqa: ASYNC240
        except BaseException:
            if os.path.exists(tmp_name):  # noqa: ASYNC240
                os.unlink(tmp_name)  # noqa: ASYNC240
            raise

        sha = hashlib.sha256(data).hexdigest()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        artifact = Artifact(
            id=new_id("art"),
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            name=final_path.name,
            kind=ArtifactKind(kind),
            mime=mime,
            size=len(data),
            sha256=sha,
            storage_path=str(final_path.relative_to(self._base_dir())),
            summary=summary[:500],
            status=ArtifactStatus(status),
            derived_from=derived_from or [],
        )
        session.add(artifact)
        await session.flush()
        await self.events.emit(
            session,
            type="artifact.created",
            actor=actor,
            run_id=run_id,
            task_key=task_key,
            attempt_id=attempt_id,
            payload={
                "artifact_id": artifact.id,
                "name": artifact.name,
                "kind": artifact.kind.value,
                "task_key": task_key or "",
                "attempt_id": attempt_id or "",
                "summary": artifact.summary,
            },
        )
        return artifact

    async def get(self, session: AsyncSession, artifact_id: str) -> Artifact | None:
        result = await session.execute(select(Artifact).where(Artifact.id == artifact_id))
        return result.scalar_one_or_none()

    async def list_for_run(self, session: AsyncSession, run_id: str) -> list[Artifact]:
        result = await session.execute(
            select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
        )
        return list(result.scalars().all())

    async def list_for_attempt(
        self, session: AsyncSession, attempt_id: str
    ) -> list[Artifact]:
        result = await session.execute(
            select(Artifact).where(Artifact.attempt_id == attempt_id)
        )
        return list(result.scalars().all())

    def read_bytes(self, artifact: Artifact) -> bytes:
        path = self._base_dir() / artifact.storage_path
        return path.read_bytes()

    async def read_content(
        self, artifact: Artifact, mode: str = "summary", *, offset: int = 0, length: int = 0
    ) -> str:
        """read_artifact 控制工具的读取实现：summary / full / range。"""
        if mode == "summary":
            return artifact.summary
        data = self.read_bytes(artifact)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"<二进制文件 {artifact.name}，{artifact.size} 字节，sha256={artifact.sha256[:12]}…>"
        if mode == "range":
            end = offset + length if length > 0 else len(text)
            return text[offset:end]
        # full：超过 30k token 截断并提示改用 range（§5.2）
        if estimate_tokens(text) > MAX_FULL_READ_TOKENS:
            truncated = text[: MAX_FULL_READ_TOKENS * 3]
            return truncated + "\n\n…[内容过长已截断，请用 range 模式分段读取]"
        return text

    async def set_status(
        self,
        session: AsyncSession,
        artifact: Artifact,
        status: ArtifactStatus,
        *,
        run_id: str | None = None,
        task_key: str | None = None,
        actor: str = "system",
    ) -> Artifact:
        old = artifact.status
        artifact.status = status
        await session.flush()
        if old != status and run_id:
            await self.events.emit(
                session,
                type="artifact.status_changed",
                actor=actor,
                run_id=run_id,
                task_key=task_key,
                payload={"artifact_id": artifact.id, "from_status": old.value, "to_status": status.value},
            )
        return artifact

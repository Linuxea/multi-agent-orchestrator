"""用户回答（拆分自 plans，路径 /runs/{id}/answers）。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.bootstrap import get_container

router = APIRouter(tags=["answers"])


class AnswerBody(BaseModel):
    question_id: str | None = None
    answer: str


@router.post("/runs/{run_id}/answers", status_code=204)
async def submit_answer(run_id: str, body: AnswerBody) -> None:
    await get_container().engine.submit_answer(run_id, body.answer, body.question_id)

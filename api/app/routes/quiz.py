"""Quiz endpoints (S5): GET /api/quiz/questions, POST /api/quiz/answer. Logic: app/quiz.py."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from ..auth import CurrentUser
from ..bodies import read_json
from ..quiz import QuizService
from ..ratelimit import rate_limit

router = APIRouter(prefix="/api/quiz", tags=["quiz"])
MAX_BODY_BYTES = 16 * 1024


def get_quiz(request: Request) -> QuizService:
    return QuizService(request.app.state.sessionmaker)


Quiz = Annotated[QuizService, Depends(get_quiz)]


@router.get("/questions", dependencies=[Depends(rate_limit("quiz"))])
async def questions(user: CurrentUser, quiz: Quiz) -> dict[str, Any]:
    """The question bank WITHOUT correct answers + the player's progress."""
    return await quiz.questions(user)


@router.post("/answer", dependencies=[Depends(rate_limit("quiz"))])
async def answer(request: Request, user: CurrentUser, quiz: Quiz) -> dict[str, Any]:
    """Batch `[{questionId, answer}]` (or `{answers: [...]}`): per-answer correctness + score. All-or-nothing.
    422 unknown_question | invalid_answer_format | validation_error, 409 quiz_already_completed."""
    return await quiz.answer(user, await read_json(request, MAX_BODY_BYTES))

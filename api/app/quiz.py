"""Quiz about the company (S5): questions without answers, batch answering with server-side checking.

Correct answers live in app/quiz_bank.py only. One attempt per question: answers are final (re-answering -> 409
quiz_already_completed), a batch is all-or-nothing. Independent of the sim.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import quiz_bank
from .bodies import invalid
from .errors import AppError
from .leads import TAG_QUIZ, high_quiz, quiz_counts
from .models import Lead, QuizAnswer, User

log = logging.getLogger("mosprom.quiz")

MAX_BATCH = quiz_bank.TOTAL


def parse_answers(payload: Any) -> list[tuple[str, int]]:
    """`[{questionId, answer}]` or `{answers: [...]}` -> [(question_id, option_index)].
    422 validation_error (shape, empty, duplicates), unknown_question, invalid_answer_format."""
    items = payload.get("answers") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise invalid("answers", "Expected a non-empty list of {questionId, answer}")
    if len(items) > MAX_BATCH:
        raise invalid("answers", f"At most {MAX_BATCH} answers per request")
    out: list[tuple[str, int]] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise invalid(f"answers.{i}", "Expected an object {questionId, answer}")
        qid = it.get("questionId", it.get("question_id"))
        if not isinstance(qid, str) or not qid:
            raise invalid(f"answers.{i}.questionId", "questionId is required (string)")
        q = quiz_bank.BY_ID.get(qid)
        if q is None:
            raise AppError("unknown_question", f"Unknown question {qid!r}", details={"question_id": qid, "index": i})
        ans = it.get("answer")
        if isinstance(ans, bool) or not isinstance(ans, int) or not 0 <= ans < len(q.options):
            raise AppError("invalid_answer_format", "answer must be the index of one of the options",
                           details={"question_id": qid, "index": i, "options": len(q.options)})
        if any(qid == prev for prev, _ in out):
            raise invalid(f"answers.{i}.questionId", "Duplicate question in the batch")
        out.append((qid, ans))
    return out


def quiz_progress(correct: int, answered: int) -> dict[str, Any]:
    return {"score": correct, "answered": answered, "total": quiz_bank.TOTAL, "completed": answered >= quiz_bank.TOTAL}


async def quiz_result(session: AsyncSession, user_id: int) -> dict[str, Any] | None:
    """`quizResult` of the season summary; null while nothing was answered."""
    correct, answered = await quiz_counts(session, user_id)
    return quiz_progress(correct, answered) if answered else None


class QuizService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None) -> None:
        if sessionmaker is None:
            raise AppError("config_error", "DATABASE_URL is not configured")
        self.sm = sessionmaker

    async def questions(self, user: User) -> dict[str, Any]:
        async with self.sm() as session:
            rows = (await session.execute(select(QuizAnswer.question_id, QuizAnswer.correct)
                                          .where(QuizAnswer.user_id == user.id))).all()
        return {"questions": [q.public() for q in quiz_bank.QUESTIONS],
                "progress": {**quiz_progress(sum(1 for r in rows if r.correct), len(rows)),
                             "answeredIds": sorted(r.question_id for r in rows)}}

    async def answer(self, user: User, payload: Any) -> dict[str, Any]:
        answers = parse_answers(payload)  # 422 first, then the state checks
        async with self.sm() as session, session.begin():
            done = set((await session.execute(select(QuizAnswer.question_id).where(QuizAnswer.user_id == user.id))
                        ).scalars())
            again = [qid for qid, _ in answers if qid in done]
            if again or len(done) >= quiz_bank.TOTAL:
                raise AppError("quiz_already_completed",
                               "The quiz is completed" if len(done) >= quiz_bank.TOTAL else "Already answered",
                               details={"question_ids": again, "completed": len(done) >= quiz_bank.TOTAL})
            results = []
            rows = []
            for qid, ans in answers:
                q = quiz_bank.BY_ID[qid]
                ok = ans == q.correct
                rows.append({"user_id": user.id, "question_id": qid, "correct": ok, "answer": ans})
                results.append({"questionId": qid, "correct": ok, "correctAnswer": q.correct, "explanation": q.explanation})
            stored = (await session.execute(pg_insert(QuizAnswer).values(rows)
                                            .on_conflict_do_nothing().returning(QuizAnswer.question_id))).all()
            if len(stored) != len(rows):  # a concurrent request answered one of them first: nothing is kept
                raise AppError("quiz_already_completed", "Already answered", details={"question_ids": [], "completed": False})
            correct, answered = await quiz_counts(session, user.id)
            await self._refresh_leads(session, user.id, correct)
        return {"results": results, **quiz_progress(correct, answered)}

    @staticmethod
    async def _refresh_leads(session: AsyncSession, user_id: int, correct: int) -> None:
        """Keep an existing (not revoked) lead in step with the quiz: quiz_score and the `high_quiz` tag."""
        want = high_quiz(correct)
        rows = (await session.execute(select(Lead.id, Lead.tags).where(Lead.user_id == user_id,
                                                                        Lead.consent_revoked_at.is_(None)))).all()
        for r in rows:
            tags = [t for t in (r.tags or []) if t != TAG_QUIZ] + ([TAG_QUIZ] if want else [])
            await session.execute(update(Lead).where(Lead.id == r.id)
                                  .values(quiz_score=correct, tags=tags, updated_at=func.now()))

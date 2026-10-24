"""Quiz about the company (S5, S9): the frontend's question bank, batch answering with server-side checking.

The bank is app/quiz_bank.py (generated from the vendored frontend bank). An answer is the option id ('a'|'b'|'c') or
the 0-based option index. One attempt per question: answers are final, a question that was answered before is ignored in
a later batch (it keeps its first answer) and a batch with nothing new -> 409 quiz_already_completed. Rows of question ids
that are not in the bank any more (the S5 bank, `q1978`...) are never counted. Independent of the sim.
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
    """`[{questionId|question_id, answer}]` or `{answers: [...]}` -> [(question_id, option_index)]. The answer is the
    option id ('a'|'b'|'c', what the client sends) or the 0-based index. 422 validation_error (shape, empty, duplicates),
    unknown_question, invalid_answer_format."""
    items = payload.get("answers") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise invalid("answers", "Expected a non-empty list of {question_id, answer}")
    if len(items) > MAX_BATCH:
        raise invalid("answers", f"At most {MAX_BATCH} answers per request")
    out: list[tuple[str, int]] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise invalid(f"answers.{i}", "Expected an object {question_id, answer}")
        qid = it.get("question_id", it.get("questionId"))
        if not isinstance(qid, str) or not qid:
            raise invalid(f"answers.{i}.question_id", "question_id is required (string)")
        q = quiz_bank.BY_ID.get(qid)
        if q is None:
            raise AppError("unknown_question", f"Unknown question {qid!r}", details={"question_id": qid, "index": i})
        idx = q.index_of(it.get("answer"))
        if idx is None:
            raise AppError("invalid_answer_format", "answer must be the id (a, b, c) or the index of one of the options",
                           details={"question_id": qid, "index": i, "options": list(q.option_ids)})
        if any(qid == prev for prev, _ in out):
            raise invalid(f"answers.{i}.question_id", "Duplicate question in the batch")
        out.append((qid, idx))
    return out


def percent(correct: int, total: int) -> int:
    """round(100 * correct / total), halves up like JavaScript's Math.round (Python's round() is banker's)."""
    return (200 * correct + total) // (2 * total) if total else 0


def quiz_progress(correct: int, answered: int) -> dict[str, Any]:
    """The client's QuizResult scalars: `total` is the number of questions answered so far (not the bank size, that is
    `bankTotal`), `score` is 0..100 over those. Used by POST /api/quiz/answer, GET /api/quiz/questions and the summary."""
    return {"correct": correct, "total": answered, "score": percent(correct, answered), "answered": answered,
            "completed": answered >= quiz_bank.TOTAL, "bankTotal": quiz_bank.TOTAL}


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
                                          .where(QuizAnswer.user_id == user.id,
                                                 QuizAnswer.question_id.in_(quiz_bank.IDS)))).all()
        return {"questions": [q.public() for q in quiz_bank.QUESTIONS],
                "progress": {**quiz_progress(sum(1 for r in rows if r.correct), len(rows)),
                             "answeredIds": sorted(r.question_id for r in rows)}}

    async def answer(self, user: User, payload: Any) -> dict[str, Any]:
        answers = parse_answers(payload)  # 422 first, then the state checks
        async with self.sm() as session, session.begin():
            done = set((await session.execute(select(QuizAnswer.question_id).where(
                QuizAnswer.user_id == user.id, QuizAnswer.question_id.in_(quiz_bank.IDS)))).scalars())
            fresh = [(qid, idx) for qid, idx in answers if qid not in done]
            if not fresh:
                raise self._nothing_new(answers, len(done))
            rows = [{"user_id": user.id, "question_id": qid, "correct": idx == quiz_bank.BY_ID[qid].correct, "answer": idx}
                    for qid, idx in fresh]
            stored = set((await session.execute(pg_insert(QuizAnswer).values(rows).on_conflict_do_nothing()
                                                .returning(QuizAnswer.question_id))).scalars())
            if not stored:  # a concurrent request answered all of them first
                raise self._nothing_new(answers, len(done) + len(rows))
            results = [{"question_id": r["question_id"], "questionId": r["question_id"], "correct": r["correct"],
                        "correctAnswer": quiz_bank.BY_ID[r["question_id"]].correct_id}
                       for r in rows if r["question_id"] in stored]
            correct, answered = await quiz_counts(session, user.id)
            await self._refresh_leads(session, user.id, correct)
        return {**quiz_progress(correct, answered), "results": results}

    @staticmethod
    def _nothing_new(answers: list[tuple[str, int]], answered: int) -> AppError:
        return AppError("quiz_already_completed",
                        "The quiz is completed" if answered >= quiz_bank.TOTAL else "Already answered",
                        details={"question_ids": [qid for qid, _ in answers], "completed": answered >= quiz_bank.TOTAL})

    @staticmethod
    async def _refresh_leads(session: AsyncSession, user_id: int, correct: int) -> None:
        """Keep an existing (not revoked) lead in step with the quiz: quiz_score (correct answers) and `high_quiz`."""
        want = high_quiz(correct)
        rows = (await session.execute(select(Lead.id, Lead.tags).where(Lead.user_id == user_id,
                                                                        Lead.consent_revoked_at.is_(None)))).all()
        for r in rows:
            tags = [t for t in (r.tags or []) if t != TAG_QUIZ] + ([TAG_QUIZ] if want else [])
            await session.execute(update(Lead).where(Lead.id == r.id)
                                  .values(quiz_score=correct, tags=tags, updated_at=func.now()))

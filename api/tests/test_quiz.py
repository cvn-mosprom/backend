"""S5: GET /api/quiz/questions, POST /api/quiz/answer (sim is DOWN in every test: the quiz never calls it)."""
from __future__ import annotations

import asyncio
import json

from app import quiz as quiz_mod
from app import quiz_bank
from tests.engage_helpers import (correct_answers, engage, engage_factory, post_lead, user_id,  # noqa: F401
                                  wrong_answers)
from tests.helpers import dev
from tests.test_errors import assert_error


def answer(c, body, tg: int = 1):
    return c.post("/api/quiz/answer", json=body, headers=dev(tg))


async def rows(c, tg: int = 1):
    return await c.db.fetch("SELECT question_id, correct, answer FROM quiz_answers WHERE user_id = $1 ORDER BY question_id",
                            await user_id(c, tg))


# ---------------------------------------------------------------- the bank

def test_bank_is_consistent():
    assert quiz_bank.TOTAL == 10 and len({q.id for q in quiz_bank.QUESTIONS}) == 10
    for q in quiz_bank.QUESTIONS:
        assert 3 <= len(q.options) <= 5 and len(set(q.options)) == len(q.options)
        assert 0 <= q.correct < len(q.options) and q.text.endswith("?") and q.explanation
    assert len({q.correct for q in quiz_bank.QUESTIONS}) >= 3  # the right answer is not always in the same slot


# ---------------------------------------------------------------- GET /api/quiz/questions

async def test_questions_have_no_answers(engage):
    r = await engage.get("/api/quiz/questions", headers=dev(1))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["questions"]) == 10
    for q in body["questions"]:
        assert set(q) == {"id", "text", "options"} and len(q["options"]) >= 3
    raw = r.text.lower()
    assert "correct" not in raw and "explanation" not in raw
    for q in quiz_bank.QUESTIONS:  # no museum fact (= the answer) leaks either
        assert q.explanation not in r.text
    assert body["progress"] == {"score": 0, "answered": 0, "total": 10, "completed": False, "answeredIds": []}


async def test_questions_401(engage):
    assert_error(await engage.get("/api/quiz/questions"), 401, "unauthorized")
    assert_error(await engage.post("/api/quiz/answer", json=correct_answers(1)), 401, "unauthorized")


# ---------------------------------------------------------------- POST /api/quiz/answer: happy paths

async def test_answer_batch_scores_on_the_server(engage):
    batch = correct_answers(2) + wrong_answers()[2:3]
    r = await answer(engage, batch)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["correct"] for x in body["results"]] == [True, True, False]
    assert [x["questionId"] for x in body["results"]] == [b["questionId"] for b in batch]
    third = quiz_bank.QUESTIONS[2]
    assert body["results"][2]["correctAnswer"] == third.correct and body["results"][2]["explanation"] == third.explanation
    assert {k: body[k] for k in ("score", "answered", "total", "completed")} == {
        "score": 2, "answered": 3, "total": 10, "completed": False}
    db = {r["question_id"]: (r["correct"], r["answer"]) for r in await rows(engage)}
    assert db == {"q1978": (True, 0), "q1979": (True, 1), "q1996": (False, 3)}
    progress = (await engage.get("/api/quiz/questions", headers=dev(1))).json()["progress"]
    assert progress["answered"] == 3 and progress["score"] == 2 and progress["answeredIds"] == ["q1978", "q1979", "q1996"]


async def test_answers_accepted_in_every_envelope(engage):
    assert (await answer(engage, {"answers": [{"questionId": "q1978", "answer": 0}]})).status_code == 200
    assert (await answer(engage, [{"question_id": "q1979", "answer": 1}])).status_code == 200
    assert len(await rows(engage)) == 2


async def test_full_quiz_then_completed(engage):
    r = await answer(engage, correct_answers())
    body = r.json()
    assert r.status_code == 200 and body["score"] == 10 and body["completed"] is True and body["answered"] == 10
    err = assert_error(await answer(engage, correct_answers(1)), 409, "quiz_already_completed")
    assert err["details"]["completed"] is True
    assert len(await rows(engage)) == 10


async def test_answers_are_per_user(engage):
    await answer(engage, correct_answers(3), tg=1)
    r = await answer(engage, wrong_answers(3), tg=2)
    assert r.status_code == 200 and r.json()["score"] == 0
    assert (await engage.get("/api/quiz/questions", headers=dev(1))).json()["progress"]["score"] == 3


# ---------------------------------------------------------------- 409: answers are final

async def test_reanswering_a_question_is_409_and_keeps_the_first_answer(engage):
    assert (await answer(engage, wrong_answers(1))).status_code == 200
    err = assert_error(await answer(engage, correct_answers(1)), 409, "quiz_already_completed")
    assert err["details"] == {"question_ids": ["q1978"], "completed": False}
    assert [(r["question_id"], r["correct"]) for r in await rows(engage)] == [("q1978", False)]


async def test_batch_overlapping_an_answered_question_stores_nothing(engage):
    await answer(engage, correct_answers(1))
    assert_error(await answer(engage, correct_answers(3)), 409, "quiz_already_completed")
    assert len(await rows(engage)) == 1  # q1979 / q1996 of the rejected batch were not kept


async def test_concurrent_identical_batches_one_wins(engage):
    rs = await asyncio.gather(*(answer(engage, correct_answers(5)) for _ in range(3)))
    assert sorted(r.status_code for r in rs) == [200, 409, 409]
    assert len(await rows(engage)) == 5


# ---------------------------------------------------------------- 422

async def test_unknown_question(engage):
    err = assert_error(await answer(engage, correct_answers(1) + [{"questionId": "q9999", "answer": 0}]),
                       422, "unknown_question")
    assert err["details"]["question_id"] == "q9999"
    assert await rows(engage) == []  # all-or-nothing


async def test_invalid_answer_format(engage):
    for bad in ("0", 1.5, True, None, -1, 4, 99, [0], {"i": 0}):
        err = assert_error(await answer(engage, [{"questionId": "q1978", "answer": bad}]), 422, "invalid_answer_format")
        assert err["details"]["question_id"] == "q1978"
    assert_error(await answer(engage, [{"questionId": "q1978"}]), 422, "invalid_answer_format")  # no answer at all
    assert await rows(engage) == []


async def test_validation_errors(engage):
    for body in ([], {}, {"answers": []}, {"answers": "q1978"}, "q1978", 5, None, [1, 2], [{"answer": 0}],
                 [{"questionId": 5, "answer": 0}], [{"questionId": "", "answer": 0}],
                 [{"questionId": "q1978", "answer": 0}] * 2,  # duplicate in one batch
                 correct_answers() + correct_answers(1)):  # more than the bank
        assert_error(await answer(engage, body), 422, "validation_error")
    r = await engage.post("/api/quiz/answer", content=b"[{", headers={**dev(1), "content-type": "application/json"})
    assert_error(r, 422, "validation_error")
    assert await rows(engage) == []


async def test_oversized_body_413(engage):
    r = await engage.post("/api/quiz/answer", content=b"[" + b" " * 20_000 + b"]",
                          headers={**dev(1), "content-type": "application/json"})
    assert_error(r, 413, "payload_too_large")


# ---------------------------------------------------------------- 429 / 500

async def test_429_after_30_requests_a_minute(engage_factory):
    c = await engage_factory(rate_limit_enabled=True)
    for i in range(30):
        assert (await c.get("/api/quiz/questions", headers=dev(1))).status_code == 200, i
    r = await c.post("/api/quiz/answer", json=correct_answers(1), headers=dev(1))  # same bucket
    err = assert_error(r, 429, "rate_limited")
    assert int(r.headers["retry-after"]) >= 1 and err["details"]["limit"] == 30


async def test_500_is_the_unified_error(engage, monkeypatch):
    async def boom(self, user, payload):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(quiz_mod.QuizService, "answer", boom)
    err = assert_error(await answer(engage, correct_answers(1)), 500, "internal_error")
    assert "secret" not in json.dumps(err)


# ---------------------------------------------------------------- the quiz feeds the lead

async def test_quiz_after_the_lead_updates_score_and_tag(engage):
    lead = (await post_lead(engage)).json()
    assert lead["quizScore"] is None and "high_quiz" not in lead["tags"]
    await answer(engage, correct_answers(7))
    me = (await engage.get("/api/leads/me", headers=dev(1))).json()
    assert me["quizScore"] == 7 and "high_quiz" not in me["tags"]
    await answer(engage, correct_answers()[7:])
    me = (await engage.get("/api/leads/me", headers=dev(1))).json()
    assert me["quizScore"] == 10 and "high_quiz" in me["tags"] and me["tags"].count("high_quiz") == 1


async def test_a_revoked_lead_is_left_alone(engage):
    await post_lead(engage)
    await engage.delete("/api/leads/me", headers=dev(1))
    await answer(engage, correct_answers())
    assert await engage.db.fetchval("SELECT quiz_score FROM leads") is None


async def test_lead_created_after_a_bad_quiz_has_no_high_tag(engage):
    await answer(engage, wrong_answers())
    lead = (await post_lead(engage)).json()
    assert lead["quizScore"] == 0 and "high_quiz" not in lead["tags"]

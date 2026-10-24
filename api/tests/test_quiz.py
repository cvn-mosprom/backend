"""S5, S9: GET /api/quiz/questions, POST /api/quiz/answer (sim is DOWN in every test: the quiz never calls it).

S9: the bank is the frontend's (q_year...q_space, options a|b|c); answers come as the option id or the index."""
from __future__ import annotations

import asyncio
import json

from app import leads as leads_mod
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
    assert quiz_bank.TOTAL == 8 and len({q.id for q in quiz_bank.QUESTIONS}) == 8
    assert quiz_bank.QUESTIONS[0].id == "q_year" and quiz_bank.QUESTIONS[-1].id == "q_space"
    for q in quiz_bank.QUESTIONS:
        assert len(q.options) == 3 and q.option_ids == ("a", "b", "c")
        assert len(set(q.options)) == 3 and q.text.endswith("?")
        assert 0 <= q.correct < 3 and q.correct_id == "abc"[q.correct]
    assert len({q.correct for q in quiz_bank.QUESTIONS}) >= 3  # the right answer is not always in the same slot
    assert quiz_bank.IDS == tuple(q.id for q in quiz_bank.QUESTIONS)
    assert quiz_bank.BY_ID["q_year"].correct_id == "b"  # 1978


def test_high_score_is_6_of_8():
    assert quiz_bank.HIGH_SCORE == 0.75
    assert [n for n in range(9) if leads_mod.high_quiz(n)] == [6, 7, 8]


def test_option_index_of():
    q = quiz_bank.BY_ID["q_year"]  # 1968 | 1978 | 1988, the right one is 'b'
    assert [q.index_of(a) for a in ("a", "b", "c", "B", " c ", 0, 1, 2)] == [0, 1, 2, 1, 2, 0, 1, 2]
    for bad in ("d", "", "ab", "0", "1", 3, -1, 99, True, False, None, 1.0, [1], {"id": "a"}):
        assert q.index_of(bad) is None, bad


def test_percent_rounds_halves_up_like_js_math_round():
    assert [quiz_mod.percent(c, 8) for c in range(9)] == [0, 13, 25, 38, 50, 63, 75, 88, 100]  # python round(): 12, 38, 62, 88
    assert [quiz_mod.percent(c, 3) for c in range(4)] == [0, 33, 67, 100]
    assert quiz_mod.percent(0, 0) == 0 and quiz_mod.percent(1, 1) == 100


# ---------------------------------------------------------------- GET /api/quiz/questions

async def test_questions_have_no_answers(engage):
    r = await engage.get("/api/quiz/questions", headers=dev(1))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [q["id"] for q in body["questions"]] == list(quiz_bank.IDS)
    for q in body["questions"]:
        assert set(q) == {"id", "text", "options", "optionIds"} and q["optionIds"] == ["a", "b", "c"]
    assert body["progress"] == {"correct": 0, "total": 0, "score": 0, "answered": 0, "completed": False,
                                "bankTotal": 8, "answeredIds": []}


async def test_questions_401(engage):
    assert_error(await engage.get("/api/quiz/questions"), 401, "unauthorized")
    assert_error(await engage.post("/api/quiz/answer", json=correct_answers(1)), 401, "unauthorized")


# ---------------------------------------------------------------- POST /api/quiz/answer: the client's form

async def test_client_request_and_response_shape(engage):
    """Exactly what vendor/frontend HttpGameApi.quizAnswer sends / reads (http-contract.md, POST /api/quiz/answer)."""
    body = {"answers": [{"question_id": "q_year", "answer": "b"}, {"question_id": "q_olymp", "answer": "c"},
                        {"question_id": "q_kvas", "answer": "c"}]}  # right, wrong (the right one is 'a'), right
    r = await answer(engage, body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["correct"], out["total"], out["score"]) == (2, 3, 67)  # the contract's own example
    assert out["results"] == [{"question_id": "q_year", "questionId": "q_year", "correct": True, "correctAnswer": "b"},
                              {"question_id": "q_olymp", "questionId": "q_olymp", "correct": False, "correctAnswer": "a"},
                              {"question_id": "q_kvas", "questionId": "q_kvas", "correct": True, "correctAnswer": "c"}]
    assert {k: out[k] for k in ("answered", "completed", "bankTotal")} == {"answered": 3, "completed": False, "bankTotal": 8}
    db = {r["question_id"]: (r["correct"], r["answer"]) for r in await rows(engage)}
    assert db == {"q_year": (True, 1), "q_olymp": (False, 2), "q_kvas": (True, 2)}  # the letter is stored as the index


async def test_letter_and_index_answers_are_the_same(engage):
    letters = await answer(engage, correct_answers(5) + wrong_answers()[5:6], tg=1)
    indexes = await answer(engage, correct_answers(5, index=True) + wrong_answers(index=True)[5:6], tg=2)
    assert letters.status_code == indexes.status_code == 200
    a, b = letters.json(), indexes.json()
    assert a == b and a["results"][-1]["correct"] is False and a["correct"] == 5 and a["total"] == 6
    assert [tuple(r) for r in await rows(engage, 1)] == [tuple(r) for r in await rows(engage, 2)]
    upper = await answer(engage, [{"question_id": "q_space", "answer": "A"}], tg=3)  # q_space: the right one is 'a'
    assert upper.status_code == 200 and upper.json()["correct"] == 1


async def test_score_is_over_the_answered_questions(engage):
    first = (await answer(engage, correct_answers(2) + wrong_answers()[2:3])).json()  # 2 right of 3
    assert (first["correct"], first["total"], first["score"]) == (2, 3, 67)
    second = (await answer(engage, correct_answers()[3:])).json()  # 5 more, all right: 7 of 8 in all
    assert (second["correct"], second["total"], second["score"], second["completed"]) == (7, 8, 88, True)
    assert len(second["results"]) == 5 and all(x["correct"] for x in second["results"])


async def test_score_math_for_a_whole_quiz(engage):
    batch = correct_answers(5) + wrong_answers()[5:]  # 5 right, 3 wrong
    out = (await answer(engage, batch)).json()
    assert (out["correct"], out["total"], out["score"], out["answered"], out["completed"]) == (5, 8, 63, 8, True)
    assert out["results"] == [{"question_id": q.id, "questionId": q.id, "correct": i < 5, "correctAnswer": q.correct_id}
                              for i, q in enumerate(quiz_bank.QUESTIONS)]
    progress = (await engage.get("/api/quiz/questions", headers=dev(1))).json()["progress"]
    assert progress == {"correct": 5, "total": 8, "score": 63, "answered": 8, "completed": True, "bankTotal": 8,
                        "answeredIds": sorted(quiz_bank.IDS)}


async def test_answers_accepted_in_every_envelope(engage):
    assert (await answer(engage, {"answers": [{"questionId": "q_year", "answer": "b"}]})).status_code == 200
    assert (await answer(engage, [{"question_id": "q_olymp", "answer": 0}])).status_code == 200
    assert (await answer(engage, {"answers": [{"question_id": "q_kvas", "answer": "c"}]})).status_code == 200
    assert len(await rows(engage)) == 3


async def test_full_quiz_then_completed(engage):
    r = await answer(engage, correct_answers())
    body = r.json()
    assert r.status_code == 200 and body["score"] == 100 and body["completed"] is True and body["answered"] == 8
    err = assert_error(await answer(engage, correct_answers(1)), 409, "quiz_already_completed")
    assert err["details"] == {"question_ids": ["q_year"], "completed": True}
    assert_error(await answer(engage, wrong_answers()), 409, "quiz_already_completed")  # a repeat never re-scores
    assert len(await rows(engage)) == 8 and all(r["correct"] for r in await rows(engage))


async def test_answers_are_per_user(engage):
    await answer(engage, correct_answers(3), tg=1)
    r = await answer(engage, wrong_answers(3), tg=2)
    assert r.status_code == 200 and r.json()["score"] == 0
    assert (await engage.get("/api/quiz/questions", headers=dev(1))).json()["progress"]["correct"] == 3


# ---------------------------------------------------------------- 409: answers are final

async def test_reanswering_a_question_is_409_and_keeps_the_first_answer(engage):
    assert (await answer(engage, wrong_answers(1))).status_code == 200
    err = assert_error(await answer(engage, correct_answers(1)), 409, "quiz_already_completed")
    assert err["details"] == {"question_ids": ["q_year"], "completed": False}
    assert [(r["question_id"], r["correct"]) for r in await rows(engage)] == [("q_year", False)]


async def test_answered_questions_in_a_batch_are_skipped_not_rescored(engage):
    """Like the client's reference server: only new questions are graded (the quiz screen resends all it has picked)."""
    await answer(engage, wrong_answers(1))
    r = await answer(engage, correct_answers(3))  # q_year is old (stays wrong), q_olymp and q_kvas are new
    assert r.status_code == 200, r.text
    out = r.json()
    assert [x["question_id"] for x in out["results"]] == ["q_olymp", "q_kvas"]
    assert (out["correct"], out["total"], out["score"]) == (2, 3, 67)
    assert [(x["question_id"], x["correct"]) for x in await rows(engage)] == [
        ("q_kvas", True), ("q_olymp", True), ("q_year", False)]


async def test_concurrent_identical_batches_one_wins(engage):
    rs = await asyncio.gather(*(answer(engage, correct_answers(5)) for _ in range(3)))
    assert sorted(r.status_code for r in rs) == [200, 409, 409]
    assert len(await rows(engage)) == 5


async def test_rows_of_the_old_bank_are_ignored(engage):
    """The S5 bank had ids q1978...: such rows stay in quiz_answers but count nowhere (quiz, lead, summary, 409)."""
    assert (await engage.get("/api/me", headers=dev(1))).status_code == 200
    uid = await user_id(engage, 1)
    for q in ("q1978", "q1979", "q1996", "q1999", "q2000", "q2003", "q2005", "q2013", "q2015", "q2022"):
        await engage.db.execute("INSERT INTO quiz_answers (user_id, question_id, correct, answer) VALUES ($1, $2, true, 0)", uid, q)
    progress = (await engage.get("/api/quiz/questions", headers=dev(1))).json()["progress"]
    assert progress["answered"] == 0 and progress["correct"] == 0 and progress["answeredIds"] == [] and not progress["completed"]
    lead = (await post_lead(engage)).json()
    assert lead["quizScore"] is None and "high_quiz" not in lead["tags"]
    r = await answer(engage, wrong_answers(2))  # not blocked by the 10 old rows, scored from zero
    assert r.status_code == 200 and (r.json()["correct"], r.json()["total"], r.json()["completed"]) == (0, 2, False)
    assert (await engage.get("/api/leads/me", headers=dev(1))).json()["quizScore"] == 0


# ---------------------------------------------------------------- 422

async def test_unknown_question(engage):
    err = assert_error(await answer(engage, correct_answers(1) + [{"question_id": "q9999", "answer": "a"}]),
                       422, "unknown_question")
    assert err["details"]["question_id"] == "q9999"
    assert_error(await answer(engage, [{"question_id": "q1978", "answer": 0}]), 422, "unknown_question")  # the old bank
    assert await rows(engage) == []  # all-or-nothing


async def test_invalid_answer_format(engage):
    for bad in ("0", "d", "", "ab", 1.5, True, None, -1, 3, 99, [0], {"i": 0}):
        err = assert_error(await answer(engage, [{"question_id": "q_year", "answer": bad}]), 422, "invalid_answer_format")
        assert err["details"]["question_id"] == "q_year" and err["details"]["options"] == ["a", "b", "c"]
    assert_error(await answer(engage, [{"question_id": "q_year"}]), 422, "invalid_answer_format")  # no answer at all
    assert await rows(engage) == []


async def test_validation_errors(engage):
    for body in ([], {}, {"answers": []}, {"answers": "q_year"}, "q_year", 5, None, [1, 2], [{"answer": "a"}],
                 [{"question_id": 5, "answer": "a"}], [{"question_id": "", "answer": "a"}],
                 [{"question_id": "q_year", "answer": "a"}] * 2,  # duplicate in one batch
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
    await answer(engage, correct_answers(5))
    me = (await engage.get("/api/leads/me", headers=dev(1))).json()
    assert me["quizScore"] == 5 and "high_quiz" not in me["tags"]
    await answer(engage, correct_answers()[5:])  # 6 of 8 right is the line
    me = (await engage.get("/api/leads/me", headers=dev(1))).json()
    assert me["quizScore"] == 8 and "high_quiz" in me["tags"] and me["tags"].count("high_quiz") == 1


async def test_a_revoked_lead_is_left_alone(engage):
    await post_lead(engage)
    await engage.delete("/api/leads/me", headers=dev(1))
    await answer(engage, correct_answers())
    assert await engage.db.fetchval("SELECT quiz_score FROM leads") is None


async def test_lead_created_after_a_bad_quiz_has_no_high_tag(engage):
    await answer(engage, wrong_answers())
    lead = (await post_lead(engage)).json()
    assert lead["quizScore"] == 0 and "high_quiz" not in lead["tags"]

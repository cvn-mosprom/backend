"""S5 against the running stack (caddy -> api -> db): leads (18+, consent, one per season, tags, revoke), quiz, events.

Leads, quiz and events never touch the sim. `test_*_with_sim_stopped` proves it by stopping the sim container (the
"lead reaches the DB with a dead sim" acceptance); it runs `docker compose stop/start sim` in the repo root, so like
test_s7_resilience.py it needs E2E_DOCKER=1 and must not run while somebody else uses the stack:

    E2E_DOCKER=1 .venv/Scripts/python -m pytest test_s5_leads_quiz_events.py -v

Needs the API with migration 0006 (rebuild: SIM_COMMIT=$(sh scripts/simcommit.sh) docker compose up -d --build api).
DB assertions go through `docker compose exec db psql` (skipped when that fails).
"""

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from conftest import BASE, Api, err

ROOT = Path(__file__).resolve().parents[1]
DOCKER = pytest.mark.skipif(os.environ.get("E2E_DOCKER") != "1", reason="set E2E_DOCKER=1 to stop/start the sim container")

# the correct options are NOT public: the e2e reads them from the bank source (pure dataclasses, no api dependencies)
sys.path.insert(0, str(ROOT / "api"))
from app import quiz_bank  # noqa: E402

CORRECT = {q.id: q.correct for q in quiz_bank.QUESTIONS}


def sql(query: str) -> str:
    try:
        out = subprocess.run(["docker", "compose", "exec", "-T", "db", "psql", "-U", "mosprom", "-d", "mosprom", "-tAc", query],
                             cwd=ROOT, capture_output=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"cannot run psql: {e}")
    if out.returncode != 0:
        pytest.skip(f"psql failed: {out.stderr[-200:]}")
    return out.stdout.strip()


def years_ago(years: int, days: int = 0) -> str:
    t = datetime.now(timezone.utc).date()
    try:
        d = t.replace(year=t.year - years)
    except ValueError:
        d = t.replace(year=t.year - years, day=28)
    return (d - timedelta(days=days)).isoformat()


def lead_body(**over):
    body = {"fullName": "Е2Е Тестов", "birthDate": years_ago(22), "phone": "+7 999 111-22-33", "telegram": "@e2e_tester",
            "university": "РХТУ им. Менделеева", "direction": "Пищевая технология", "course": "4", "vacancy": "lab",
            "readiness": "season", "practiceType": "paid", "consent": True, "consentTextVersion": "2026-10-v1"}
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def post(api: Api, path: str, body) -> httpx.Response:
    return api.http.post(path, json=body)  # single shot: no 429 retries


def now_ms(offset_s: float = 0) -> int:
    return int((time.time() + offset_s) * 1000)


def ev(name="app_open", ts=None, **props):
    e = {"name": name, "ts": now_ms() if ts is None else ts}
    return {**e, "props": props} if props else e


def uid_of(api: Api) -> str:
    return sql(f"SELECT id FROM users WHERE tg_id = {api.tg_id}")


# ---------------------------------------------------------------- leads

def test_lead_happy_path_reaches_db_and_outbox(api: Api):
    r = post(api, "/api/leads", lead_body())
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["status"] == "new" and lead["phone"] == "+79991112233" and lead["telegram"] == "e2e_tester"
    assert {"senior", "profile_direction", "ready_for_season"} <= set(lead["tags"])
    me = api.http.get("/api/leads/me")
    assert me.status_code == 200 and me.json()["id"] == lead["id"] and "hrNote" not in me.json()
    uid = uid_of(api)
    row = sql(f"SELECT full_name, status, consent, consent_revoked_at IS NULL FROM leads WHERE user_id = {uid}")
    assert row == "Е2Е Тестов|new|t|t"
    kinds = sql(f"SELECT kind FROM outbox WHERE payload->>'user_id' = '{uid}' ORDER BY id")
    assert kinds == "lead_created"


def test_legacy_form_needs_age_confirmation_and_maps_contact(api: Api):
    legacy = {"fullName": "Пётр Сидоров", "contact": "@petr_sidorov", "university": "МИФИ", "course": "2", "roleId": "operator",
              "consent": True, "consentTextVersion": "2026-10-v1"}
    e = err(post(api, "/api/leads", legacy))
    assert e["code"] == "age_required"
    api.bootstrap()
    api.command({"type": "ageConfirm"})  # the confirmation lives in the sim state / snapshot
    r = post(api, "/api/leads", legacy)
    assert r.status_code == 201, r.text
    assert r.json()["telegram"] == "petr_sidorov" and r.json()["vacancy"] == "operator" and r.json()["birthDate"] is None


def test_401_everywhere():
    anon = httpx.Client(base_url=BASE, verify=False, timeout=30.0)
    try:
        for method, path in (("POST", "/api/leads"), ("GET", "/api/leads/me"), ("DELETE", "/api/leads/me"),
                             ("GET", "/api/quiz/questions"), ("POST", "/api/quiz/answer"), ("POST", "/api/events")):
            r = anon.request(method, path, json={} if method == "POST" else None)
            assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized", (method, path)
    finally:
        anon.close()


def test_404_403_errors(api: Api):  # 5 POSTs: the limit is 5 a minute per user
    assert err(api.http.get("/api/leads/me"))["code"] == "lead_not_found"
    assert err(api.http.delete("/api/leads/me"))["code"] == "lead_not_found"
    for over in ({"birthDate": years_ago(17, 100)}, {"birthDate": None}):
        r = post(api, "/api/leads", lead_body(**over))
        assert r.status_code == 403 and r.json()["error"]["code"] == "age_required", over
    for over in ({"consent": False}, {"consent": None}, {"consentTextVersion": None}):
        r = post(api, "/api/leads", lead_body(**over))
        assert r.status_code == 403 and r.json()["error"]["code"] == "consent_required", over
    assert sql(f"SELECT count(*) FROM leads WHERE user_id = {uid_of(api)}") == "0"
    assert sql(f"SELECT count(*) FROM outbox WHERE payload->>'user_id' = '{uid_of(api)}'") == "0"


def test_422_validation_errors(api: Api):
    for over in ({"fullName": None}, {"phone": None, "telegram": None}, {"practiceType": "volunteer"}, {"vacancy": "ceo"},
                 {"birthDate": "yesterday"}):
        r = post(api, "/api/leads", lead_body(**over))
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error", over
    assert sql(f"SELECT count(*) FROM leads WHERE user_id = {uid_of(api)}") == "0"


def test_409_one_lead_per_season(api: Api):
    assert post(api, "/api/leads", lead_body()).status_code == 201
    r = post(api, "/api/leads", lead_body(fullName="Дубль Дублёв"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "lead_already_exists"
    uid = uid_of(api)
    assert sql(f"SELECT count(*) FROM outbox WHERE payload->>'user_id' = '{uid}'") == "1"
    assert sql(f"SELECT full_name FROM leads WHERE user_id = {uid}") == "Е2Е Тестов"


def test_revoke_is_idempotent_and_leaves_the_funnel(api: Api):
    assert post(api, "/api/leads", lead_body()).status_code == 201
    assert api.http.delete("/api/leads/me").status_code == 204
    uid = uid_of(api)
    first = sql(f"SELECT consent_revoked_at FROM leads WHERE user_id = {uid}")
    assert first and sql(f"SELECT status FROM leads WHERE user_id = {uid}") == "new"
    assert api.http.delete("/api/leads/me").status_code == 204
    assert sql(f"SELECT consent_revoked_at FROM leads WHERE user_id = {uid}") == first
    assert sql(f"SELECT kind FROM outbox WHERE payload->>'user_id' = '{uid}' ORDER BY id") == "lead_created\nlead_revoked"
    assert api.http.get("/api/leads/me").json()["consentRevokedAt"] is not None


def test_lead_rate_limit_is_5_per_minute(api: Api):
    codes = [post(api, "/api/leads", lead_body(fullName=None)).status_code for _ in range(5)]
    assert codes == [422] * 5
    r = post(api, "/api/leads", lead_body(fullName=None))
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1


# ---------------------------------------------------------------- quiz

def test_quiz_flow_and_summary(api: Api):
    api.bootstrap()
    q = api.http.get("/api/quiz/questions")
    assert q.status_code == 200
    body = q.json()
    assert len(body["questions"]) == 10 and all(set(x) == {"id", "text", "options"} for x in body["questions"])
    assert "correct" not in q.text and body["progress"]["answered"] == 0
    assert {x["id"] for x in body["questions"]} == set(CORRECT)
    first = [{"questionId": qid, "answer": a} for qid, a in list(CORRECT.items())[:3]]
    first[2]["answer"] = (first[2]["answer"] + 1) % 4  # one wrong
    r = post(api, "/api/quiz/answer", {"answers": first})
    assert r.status_code == 200, r.text
    assert [x["correct"] for x in r.json()["results"]] == [True, True, False] and r.json()["score"] == 2
    e = err(post(api, "/api/quiz/answer", [{"questionId": "q1978", "answer": 0}]))
    assert e["code"] == "quiz_already_completed"
    assert err(post(api, "/api/quiz/answer", [{"questionId": "nope", "answer": 0}]))["code"] == "unknown_question"
    assert err(post(api, "/api/quiz/answer", [{"questionId": "q2000", "answer": 9}]))["code"] == "invalid_answer_format"
    rest = [{"questionId": qid, "answer": a} for qid, a in list(CORRECT.items())[3:]]
    r = post(api, "/api/quiz/answer", rest)
    assert r.status_code == 200 and r.json()["completed"] is True and r.json()["score"] == 9
    assert post(api, "/api/leads", lead_body()).json()["quizScore"] == 9
    s = api.http.get("/api/season/summary").json()
    assert s["quizResult"] == {"score": 9, "answered": 10, "total": 10, "completed": True}
    assert s["leadStatus"] == "new"
    assert "high_quiz" in api.http.get("/api/leads/me").json()["tags"]
    assert err(post(api, "/api/quiz/answer", rest[:1]))["code"] == "quiz_already_completed"


# ---------------------------------------------------------------- events

def test_events_batch_and_errors(api: Api):
    api.bootstrap()
    assert post(api, "/api/events", {"events": [ev(), ev("game_end", result="win", duration_ms=4200)]}).json() == {"accepted": 2}
    uid = uid_of(api)
    assert sql(f"SELECT count(*) FROM events WHERE user_id = {uid}") == "2"
    assert sql(f"SELECT props->>'result' FROM events WHERE user_id = {uid} AND name = 'game_end'") == "win"
    assert err(post(api, "/api/events", {"events": [ev("hack")]}))["code"] == "invalid_event_name"
    assert err(post(api, "/api/events", {"events": [ev(ts=now_ms(300))]}))["code"] == "future_timestamp"
    assert err(post(api, "/api/events", {"events": [ev() for _ in range(51)]}))["code"] == "too_many_events"
    assert post(api, "/api/events", {"events": [ev() for _ in range(50)]}).json() == {"accepted": 50}
    assert sql(f"SELECT count(*) FROM events WHERE user_id = {uid}") == "52"  # rejected batches stored nothing


def test_events_rate_limit_is_10_batches_per_minute(api: Api):
    for _ in range(10):
        assert post(api, "/api/events", {"events": [ev()]}).status_code == 200
    r = post(api, "/api/events", {"events": [ev()]})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1


# ---------------------------------------------------------------- with the sim container stopped

def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, timeout=240)


def wait_ready(timeout_s: float = 150.0) -> None:
    deadline = time.monotonic() + timeout_s
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{BASE}/ready", verify=False, timeout=5.0)
            if r.status_code == 200:
                return
            last = f"{r.status_code} {r.text[:200]}"
        except httpx.HTTPError as e:
            last = type(e).__name__
        time.sleep(2)
    raise AssertionError(f"stack not ready after {timeout_s}s: {last}")


def wait_sim_healthy(timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    health = ""
    while time.monotonic() < deadline:
        out = subprocess.run(["docker", "compose", "ps", "sim", "--format", "{{.Health}}"], cwd=ROOT, capture_output=True,
                             encoding="utf-8", timeout=60)
        health = out.stdout.strip()
        if health == "healthy":
            return
        time.sleep(2)
    raise AssertionError(f"sim container not healthy after {timeout_s}s: {health!r}")


@pytest.fixture
def sim_stopped():
    """`docker compose stop sim` for the test; ALWAYS started again (and waited for) afterwards, even on failure."""
    try:
        compose("stop", "sim")
        yield
    finally:
        compose("start", "sim")
        wait_sim_healthy()
        wait_ready()


@DOCKER
def test_lead_quiz_events_with_sim_stopped(api: Api, sim_stopped):
    r = api.http.get("/api/bootstrap")
    assert r.status_code == 503 and r.json()["error"]["code"] == "sim_unavailable"  # the sim is really gone
    r = post(api, "/api/leads", lead_body(fullName="Лид При Мёртвом Sim"))
    assert r.status_code == 201, r.text
    uid = uid_of(api)
    assert sql(f"SELECT full_name FROM leads WHERE user_id = {uid}") == "Лид При Мёртвом Sim"  # reached the DB
    assert sql(f"SELECT kind FROM outbox WHERE payload->>'user_id' = '{uid}'") == "lead_created"
    assert api.http.get("/api/leads/me").status_code == 200
    q = post(api, "/api/quiz/answer", [{"questionId": "q1978", "answer": 0}])
    assert q.status_code == 200 and q.json()["score"] == 1
    assert api.http.get("/api/quiz/questions").status_code == 200
    assert post(api, "/api/events", {"events": [ev("lead_submit", role="lab")]}).json() == {"accepted": 1}
    assert sql(f"SELECT count(*) FROM events WHERE user_id = {uid}") == "1"
    assert api.http.delete("/api/leads/me").status_code == 204
    assert api.http.delete("/api/leads/me").status_code == 204

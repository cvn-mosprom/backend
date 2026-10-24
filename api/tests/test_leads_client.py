"""S9: the lead form in the client's shape (vendor/frontend LeadForm / buildLead), department mapping, institution kind,
the LeadInfo of GET /api/leads/me, migration 0008. The sim is DOWN in every test (the `engage` fixture asserts it)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import date

import pytest

from app import leads as leads_mod
from app.leads import DEPARTMENTS, LeadIn, clean, compute_tags
from tests.engage_helpers import (client_body, engage, engage_factory, lead_body, outbox, user_id,  # noqa: F401
                                  years_ago)
from tests.helpers import API_DIR, TEST_DB_URL, dev
from tests.test_errors import assert_error

ON = date(2026, 10, 24)


async def post_client(c, tg: int = 1, **over):
    return await c.post("/api/leads", json=client_body(**over), headers=dev(tg))


def cleaned(**over):
    return clean(LeadIn(**client_body(**over)), ON)


# ---------------------------------------------------------------- the client's form, as it is

async def test_client_lead_form_exactly_as_the_client_sends_it(engage):
    """vendor/frontend docs/http-contract.md: the example body of POST /api/leads, with one contact (Telegram)."""
    body = {"fullName": "Иван Иванов", "birthDate": years_ago(21), "tgUsername": "ivan", "university": "МГУ",
            "institutionKind": "vuz", "course": "3", "department": "lab", "roleId": "operator",
            "readyFrom": "2027-03-01", "practiceType": "paid", "consent": True, "consentVersion": "2026-10-v1"}
    r = await engage.post("/api/leads", json=body, headers=dev(1))
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["ok"] is True and lead["status"] == "new" and lead["roleId"] == "operator" == lead["vacancy"]
    assert lead["telegram"] == "ivan" and lead["phone"] is None and lead["email"] is None
    assert lead["institutionKind"] == "vuz" and lead["direction"] == "Лаборатория"
    assert lead["consentTextVersion"] == "2026-10-v1" and lead["practiceType"] == "paid"
    assert lead["readyFrom"] == "2027-03-01" and lead["course"] == "3"
    assert "profile_direction" in lead["tags"] and "senior" not in lead["tags"]
    row = await engage.db.fetchrow("SELECT telegram, direction, institution_kind, consent_text_version, vacancy FROM leads")
    assert dict(row) == {"telegram": "ivan", "direction": "Лаборатория", "institution_kind": "vuz",
                         "consent_text_version": "2026-10-v1", "vacancy": "operator"}


async def test_client_form_with_all_three_contacts_and_minimum_fields(engage):
    r = await post_client(engage)
    assert r.status_code == 201, r.text
    assert (r.json()["phone"], r.json()["email"], r.json()["telegram"]) == ("+79990000000", "a@b.ru", "ivan_ivanov")
    # buildLead() leaves out the empty optionals; institutionKind and readyFrom are optional too
    r = await post_client(engage, 2, phone=None, email=None, institutionKind=None, readyFrom=None, tgUsername="@petr_ivanov")
    assert r.status_code == 201, r.text
    assert r.json()["telegram"] == "petr_ivanov" and r.json()["institutionKind"] is None and r.json()["readyFrom"] is None


@pytest.mark.parametrize("field,client,stored,value", [
    ("consentVersion", "consentVersion", "consent_text_version", "2026-10-24"),
    ("tgUsername", "tgUsername", "telegram", "@ivan_tg"),
    ("department", "department", "direction", "bottling"),
])
async def test_each_new_alias_alone(engage, field, client, stored, value):
    """Start from the pre-S9 body (old names) and swap exactly one old name for the client's."""
    old = {"consentVersion": "consentTextVersion", "tgUsername": "telegram", "department": "direction"}[field]
    body = lead_body(**{old: None, client: value})
    assert old not in body and client in body
    r = await engage.post("/api/leads", json=body, headers=dev(1))
    assert r.status_code == 201, r.text
    got = await engage.db.fetchval(f"SELECT {stored} FROM leads")
    assert got == {"@ivan_tg": "ivan_tg", "bottling": "Розлив"}.get(value, value)


async def test_old_names_still_work(engage):
    r = await engage.post("/api/leads", json=lead_body(), headers=dev(1))
    assert r.status_code == 201, r.text
    assert r.json()["telegram"] == "ivan_petrov" and r.json()["direction"] == "Химическая технология"
    assert r.json()["consentTextVersion"] == "2026-10-v1" and r.json()["institutionKind"] is None


async def test_snake_case_client_fields(engage):
    r = await engage.post("/api/leads", headers=dev(1), json={
        "full_name": "Анна Петрова", "birth_date": years_ago(30), "tg_username": "anna_pet", "university": "МИФИ",
        "institution_kind": "college", "department": "tech", "role_id": "adjuster", "practice_type": "job",
        "consent": True, "consent_version": "v9"})
    assert r.status_code == 201, r.text
    lead = r.json()
    assert (lead["telegram"], lead["institutionKind"], lead["direction"], lead["vacancy"], lead["consentTextVersion"]) == (
        "anna_pet", "college", "Техника и наладка", "adjuster", "v9")


async def test_explicit_old_name_wins_where_both_are_sent(engage):
    r = await post_client(engage, telegram="real_one", tgUsername="other_one", consentTextVersion="old-v", consentVersion="new-v")
    assert r.status_code == 201 and r.json()["telegram"] == "real_one" and r.json()["consentTextVersion"] == "old-v"


async def test_consent_version_is_still_required(engage):
    assert_error(await post_client(engage, consentVersion=None), 403, "consent_required")
    assert_error(await post_client(engage, consent=False), 403, "consent_required")


async def test_client_form_errors_keep_their_codes(engage):
    assert_error(await post_client(engage, birthDate=years_ago(17)), 403, "age_required")
    assert_error(await post_client(engage, phone=None, email=None, tgUsername=None), 422, "validation_error")
    assert_error(await post_client(engage, fullName="И"), 422, "validation_error")
    assert (await post_client(engage, 3)).status_code == 201
    assert_error(await post_client(engage, 3), 409, "lead_already_exists")


# ---------------------------------------------------------------- department -> label + tag

def test_department_table_matches_the_client_ids():
    assert list(DEPARTMENTS) == ["lab", "bottling", "tech", "warehouse", "engineering", "other"]
    assert {k: v[0] for k, v in DEPARTMENTS.items()} == {
        "lab": "Лаборатория", "bottling": "Розлив", "tech": "Техника и наладка", "warehouse": "Склад и логистика",
        "engineering": "Инженерия", "other": "Другое"}
    assert [k for k, v in DEPARTMENTS.items() if v[1] is None] == ["other"]  # only `other` gives no tag


@pytest.mark.parametrize("dept,label", [(k, v[0]) for k, v in DEPARTMENTS.items()])
async def test_department_is_stored_as_label_and_tags_by_id(engage, dept, label):
    r = await post_client(engage, department=dept, course="1", institutionKind="vuz", readyFrom=None)
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["direction"] == label
    assert ("profile_direction" in lead["tags"]) is (dept != "other")
    c = cleaned(department=dept)
    assert c.direction == label and c.profile is (dept != "other")


def test_department_wins_over_free_text_and_free_text_still_tags():
    assert cleaned(department="other", direction="Химия").direction == "Другое"
    assert "profile_direction" not in compute_tags(cleaned(department="other", direction="Химия", course="1"), 0, ON)
    free = clean(LeadIn(**lead_body(direction="Химия", course="1", readiness="later")), ON)  # no department: keywords
    assert free.direction == "Химия" and "profile_direction" in compute_tags(free, 0, ON)
    assert cleaned(department=" LAB ").direction == "Лаборатория"  # trimmed, case-free


async def test_unknown_department_and_institution_kind_are_422(engage):
    err = assert_error(await post_client(engage, department="hr"), 422, "validation_error")
    assert "department" in str(err["details"])
    err = assert_error(await post_client(engage, institutionKind="school"), 422, "validation_error")
    assert "institutionKind" in str(err["details"])
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 0


# ---------------------------------------------------------------- institution kind -> senior

def test_college_counts_for_the_senior_tag_like_a_4th_course():
    base = dict(department="other", readyFrom=None, readiness="later")
    assert compute_tags(cleaned(institutionKind="college", course="1", **base), 0, ON) == ["senior"]
    assert compute_tags(cleaned(institutionKind="vuz", course="1", **base), 0, ON) == []
    assert compute_tags(cleaned(institutionKind=None, course="1", **base), 0, ON) == []
    assert compute_tags(cleaned(institutionKind="vuz", course="4", **base), 0, ON) == ["senior"]  # unchanged
    assert compute_tags(cleaned(institutionKind="college", course="4", **base), 0, ON) == ["senior"]  # once
    assert compute_tags(cleaned(institutionKind="VUZ", course="1", **base), 0, ON) == []


async def test_college_lead_gets_the_senior_tag_and_the_kind_is_stored(engage):
    r = await post_client(engage, institutionKind="college", course="2", department="other", readyFrom=None)
    assert r.status_code == 201, r.text
    assert "senior" in r.json()["tags"] and r.json()["institutionKind"] == "college"
    assert await engage.db.fetchval("SELECT institution_kind FROM leads") == "college"
    assert (await outbox(engage, "lead_created"))[0]["payload"]["institution_kind"] == "college"


async def test_outbox_card_carries_kind_and_department_label(engage):
    await post_client(engage, department="warehouse")
    p = (await outbox(engage, "lead_created"))[0]["payload"]
    assert (p["institution_kind"], p["direction"], p["vacancy"], p["telegram"]) == (
        "vuz", "Склад и логистика", "operator", "ivan_ivanov")


# ---------------------------------------------------------------- GET /api/leads/me = LeadInfo

async def test_get_lead_me_is_the_clients_leadinfo(engage):
    t0 = int(time.time() * 1000)
    created = (await post_client(engage, department="lab", roleId="kipia")).json()
    r = await engage.get("/api/leads/me", headers=dev(1))
    assert r.status_code == 200, r.text
    me = r.json()
    # LeadInfo { status, createdAt: number (ms), roleId? }
    assert me["status"] == "new" and me["roleId"] == "kipia" and me["roleId"] == me["vacancy"]
    assert type(me["createdAt"]) is int and t0 - 5_000 <= me["createdAt"] <= int(time.time() * 1000) + 5_000
    assert date.fromtimestamp(me["createdAt"] / 1000).year >= 2026  # not "01.01.1970"
    db_created = await engage.db.fetchval("SELECT (extract(epoch FROM created_at) * 1000)::bigint FROM leads")
    assert abs(me["createdAt"] - db_created) <= 1
    # the other fields are kept
    for k in ("id", "seasonId", "tags", "fullName", "birthDate", "phone", "email", "telegram", "university", "institutionKind",
              "direction", "course", "readyFrom", "practiceType", "consent", "consentAt", "consentTextVersion",
              "consentRevokedAt", "bestProfession", "seasonPlace", "quizScore", "updatedAt"):
        assert k in me, k
    assert me == {k: v for k, v in created.items() if k != "ok"}
    assert isinstance(me["consentAt"], str) and "hrNote" not in me


async def test_leadinfo_status_follows_hr(engage):
    await post_client(engage)
    await engage.db.execute("UPDATE leads SET status = 'interview'")
    assert (await engage.get("/api/leads/me", headers=dev(1))).json()["status"] == "interview"


async def test_revoke_after_a_client_lead(engage):
    await post_client(engage)
    assert (await engage.delete("/api/leads/me", headers=dev(1))).status_code == 204
    assert (await engage.delete("/api/leads/me", headers=dev(1))).status_code == 204  # idempotent
    assert (await engage.get("/api/leads/me", headers=dev(1))).json()["consentRevokedAt"] is not None
    assert_error(await engage.delete("/api/leads/me", headers=dev(9)), 404, "lead_not_found")


# ---------------------------------------------------------------- migration 0008

def alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API_DIR, capture_output=True, text=True,
                          env={**os.environ, "DATABASE_URL": TEST_DB_URL})


def test_migration_0008_adds_the_column_and_downgrades(pg_url):
    import asyncio

    import asyncpg

    async def state() -> tuple[bool, list[str]]:
        conn = await asyncpg.connect(TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            has = await conn.fetchval("SELECT count(*) FROM information_schema.columns "
                                      "WHERE table_name = 'leads' AND column_name = 'institution_kind'")
            cons = await conn.fetch("SELECT conname FROM pg_constraint WHERE conrelid = 'leads'::regclass AND contype = 'c'")
            return bool(has), sorted(r["conname"] for r in cons)
        finally:
            await conn.close()

    assert asyncio.run(state()) == (True, ["ck_leads_institution_kind", "ck_leads_practice_type", "ck_leads_status"])
    down = alembic("downgrade", "0007")
    assert down.returncode == 0, down.stderr
    assert asyncio.run(state())[0] is False
    up = alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert asyncio.run(state())[0] is True


async def test_institution_kind_check_constraint(engage):
    import asyncpg

    await post_client(engage)
    with pytest.raises(asyncpg.CheckViolationError):
        await engage.db.execute("UPDATE leads SET institution_kind = 'school'")
    await engage.db.execute("UPDATE leads SET institution_kind = NULL")

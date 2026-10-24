"""Every route of the public API (README "Endpoints" table + /api/me): method, path, a body that is valid for it, rate-limit bucket."""

import uuid
from typing import Any

# (method, path, valid JSON body or None, rate-limit bucket)
ROUTES: list[tuple[str, str, Any, str]] = [
    ("GET", "/api/me", None, "default"),
    ("GET", "/api/bootstrap", None, "bootstrap"),
    ("POST", "/api/command", {"version": 0, "idempotency_key": uuid.uuid4().hex, "cmd": {"type": "buildEnd"}}, "command"),
    ("POST", "/api/advance", {"cap_hours": 0, "reason": "manual"}, "advance"),
    ("GET", "/api/factory/me", None, "factory_me"),
    ("GET", "/api/season/leaderboard?tab=general", None, "leaderboard"),
    ("GET", "/api/season/summary", None, "default"),
    ("GET", "/api/factory/1", None, "default"),
    ("POST", "/api/leads", {"fullName": "Smoke Test", "phone": "+79991112233", "university": "МИФИ", "birthDate": "2000-01-01",
                           "consent": True, "consentTextVersion": "2026-10-v1"}, "leads"),
    ("GET", "/api/leads/me", None, "default"),
    ("DELETE", "/api/leads/me", None, "default"),
    ("GET", "/api/quiz/questions", None, "quiz"),
    ("POST", "/api/quiz/answer", [{"question_id": "q_year", "answer": "b"}], "quiz"),
    ("POST", "/api/events", {"events": [{"name": "app_open"}]}, "events"),
]
ROUTE_IDS = [f"{m} {p.split('?')[0]}" for m, p, _, _ in ROUTES]

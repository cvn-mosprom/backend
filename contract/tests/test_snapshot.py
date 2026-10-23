"""Snapshot contract (S0 Done: "контрактный тест схемы снапшота").

Offline part: a hand-made but complete example snapshot must satisfy both the generated pydantic
model and the JSON Schema; drift in either direction (missing / extra top-level keys) is caught.

Live part (optional): if SIM_URL is set (e.g. http://127.0.0.1:58080 when the compose sim port is
published, or http://sim:8080 inside the network), a real snapshot from POST /v1/bootstrap is validated
against the same schema. This is the guard for the S1 merge (new Order fields etc.): when the real
snapshot grows a field, the frontend types.ts changes too and `contract/generate.sh` must be re-run.
"""

from __future__ import annotations

import copy
import json
import os
import urllib.error
import urllib.request

import jsonschema
import pytest
from pydantic import ValidationError


def test_snapshot_example_valid_pydantic(gen, snapshot_example):
    snap = gen.GameSnapshot.model_validate(snapshot_example)
    assert snap.factory.layout.hangar.w == 24
    assert snap.orders[0].status == "accepted"
    assert snap.model_dump(mode="json", exclude_unset=True) == snapshot_example


def test_snapshot_example_valid_json_schema(snapshot_schema, snapshot_example):
    jsonschema.validate(snapshot_example, snapshot_schema)


def test_minimal_snapshot_only_required_fields(gen, snapshot_schema, snapshot_example):
    minimal = copy.deepcopy(snapshot_example)
    for k in ("undoSteps", "redoSteps", "undoLabel", "redoLabel", "event", "offline", "rank", "loanLimit"):
        del minimal[k]
    gen.GameSnapshot.model_validate(minimal)
    jsonschema.validate(minimal, snapshot_schema)


@pytest.mark.parametrize("key", ["serverTime", "player", "factory", "orders", "season", "skills", "dialogs"])
def test_missing_required_top_level_key_rejected(gen, snapshot_schema, snapshot_example, key):
    broken = copy.deepcopy(snapshot_example)
    del broken[key]
    with pytest.raises(ValidationError):
        gen.GameSnapshot.model_validate(broken)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, snapshot_schema)


def test_unknown_field_is_flagged_as_contract_drift(gen, snapshot_schema, snapshot_example):
    """The contract is strict on purpose: a new field in the sim output must come with a types.ts change."""
    drifted = copy.deepcopy(snapshot_example)
    drifted["orders"][0]["finishAt"] = 1761260000000  # e.g. the S1 'completion moment' before types.ts has it
    with pytest.raises(ValidationError):
        gen.GameSnapshot.model_validate(drifted)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(drifted, snapshot_schema)


def test_command_result_variants(gen, snapshot_example):
    ok = {"ok": True, "snapshot": snapshot_example, "toast": "Построено", "rewards": {"credits": 10, "unlocked": ["x"]}}
    failed = {"ok": False, "error": "Не хватает 🪙"}
    failed_with_snap = {"ok": False, "error": "Конфликт", "snapshot": snapshot_example}
    for raw in (ok, failed, failed_with_snap):
        res = gen.CommandResult.model_validate(raw)
        assert res.root.ok is raw["ok"]
    with pytest.raises(ValidationError):
        gen.CommandResult.model_validate({"ok": True})  # snapshot required when ok
    with pytest.raises(ValidationError):
        gen.CommandResult.model_validate({"ok": False})  # error required when not ok


def test_snapshot_schema_exposes_command_result(snapshot_schema):
    assert snapshot_schema["$ref"] == "#/definitions/GameSnapshot"
    assert {"GameSnapshot", "CommandResult"} <= set(snapshot_schema["definitions"])


def _live_snapshot(sim_url: str) -> dict:
    body = json.dumps(
        {"state": None, "now": 1761253200000, "player": {"id": "u_contract", "name": "Contract Test"}}
    ).encode()
    req = urllib.request.Request(
        sim_url.rstrip("/") + "/v1/bootstrap", data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted local URL)
        return json.loads(resp.read())["snapshot"]


@pytest.mark.skipif(not os.environ.get("SIM_URL"), reason="SIM_URL not set (live sim snapshot check)")
def test_live_sim_snapshot_matches_contract(gen, snapshot_schema):
    try:
        snap = _live_snapshot(os.environ["SIM_URL"])
    except urllib.error.URLError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"sim not reachable: {e}")
    jsonschema.validate(snap, snapshot_schema)
    gen.GameSnapshot.model_validate(snap)


def _post(sim_url: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        sim_url.rstrip("/") + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted local URL)
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.skipif(not os.environ.get("SIM_URL"), reason="SIM_URL not set (live sim command check)")
def test_live_sim_accepts_every_example_command_and_snapshot_stays_valid(gen, snapshot_schema, command_examples):
    """Threads one factory state through all example commands: the sim must never answer 400 (bad_request)
    for a contract-valid command, and every returned snapshot must satisfy the generated schema."""
    sim_url, now = os.environ["SIM_URL"], 1761253200000
    status, boot = _post(sim_url, "/v1/bootstrap", {"state": None, "now": now, "player": {"id": "u_c", "name": "C"}})
    assert status == 200, boot
    state = boot["state"]
    for ctype, examples in command_examples.items():
        for cmd in examples:
            status, res = _post(sim_url, "/v1/command", {"state": state, "now": now, "cmd": cmd})
            assert status == 200, (ctype, res)  # game-rule rejections are 200 with result.ok == false
            assert isinstance(res["result"]["ok"], bool), (ctype, res["result"])
            jsonschema.validate(res["snapshot"], snapshot_schema)
            gen.GameSnapshot.model_validate(res["snapshot"])
            state = res["state"]

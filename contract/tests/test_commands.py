"""Command contract: every variant of the TS `Command` union parses from a realistic example."""

from __future__ import annotations

import copy
import json
import re

import jsonschema
import pytest
from conftest import SCHEMA_DIR, TYPES_TS, all_command_examples, pascal
from pydantic import ValidationError

# Block 1 of the spec: 27 working commands. `tick` left the contract in frontend rev. 4 (the server is analytic).
SPEC_COMMANDS = [
    "place", "placeConveyor", "rerouteConveyor", "remove", "move", "rotate", "configure", "upgrade",
    "upgradeConveyor", "repair", "expandHangar", "paint", "acceptOrder", "cancelOrder", "sellMarket",
    "submitShift", "takeLoan", "repayLoan", "undo", "redo", "renameFactory", "setUniversity",
    "dialogDone", "factSeen", "buildEnd", "ageConfirm", "offlineSeen",
]  # fmt: skip

EXAMPLES = all_command_examples()


def _ids(pairs):
    seen: dict[str, int] = {}
    out = []
    for t, _ in pairs:
        seen[t] = seen.get(t, 0) + 1
        out.append(f"{t}-{seen[t]}")
    return out


def test_command_types_match_spec_list(gen):
    assert len(gen.COMMAND_TYPES) == 27
    assert set(gen.COMMAND_TYPES) == set(SPEC_COMMANDS)
    assert len(set(gen.COMMAND_TYPES)) == 27
    assert "tick" not in gen.COMMAND_TYPES


@pytest.mark.skipif(not TYPES_TS.exists(), reason="vendor/frontend submodule not checked out")
def test_command_types_match_types_ts(gen):
    """Independent cross-check (regex over the TS source, not the generator): same 27 discriminators."""
    src = TYPES_TS.read_text(encoding="utf-8")
    block = src[src.index("export type Command =") : src.index("export type CommandResult")]
    from_ts = re.findall(r"\|\s*\{\s*type:\s*'(\w+)'", block)
    assert from_ts == list(gen.COMMAND_TYPES)


def test_every_command_type_has_an_example(gen, command_examples):
    assert set(command_examples) == set(gen.COMMAND_TYPES)
    for t, items in command_examples.items():
        assert items, f"no examples for {t}"
        assert all(ex["type"] == t for ex in items), t


@pytest.mark.parametrize(("ctype", "example"), EXAMPLES, ids=_ids(EXAMPLES))
def test_example_parses_to_matching_variant(gen, ctype, example):
    parsed = gen.parse_command(example)
    assert type(parsed).__name__ == pascal(ctype) + "Command"
    # round trip is lossless on the wire format (alias `from`, explicit nulls kept)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == example
    # the Command root model accepts it too
    assert gen.Command.model_validate(example).root == parsed


@pytest.mark.parametrize(("ctype", "example"), EXAMPLES, ids=_ids(EXAMPLES))
def test_example_valid_against_json_schema(command_schema, ctype, example):
    jsonschema.validate(example, command_schema)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "tickk"},
        {"type": "tick", "from": 1, "to": 2, "events": []},  # removed from the contract: 422 validation_error at the API
        {"type": "nope", "x": 1},
        {"type": ""},
        {"type": None},
        {"type": 42},
        {},
        {"kind": "place"},
    ],
)
def test_unknown_or_missing_type_rejected(gen, command_schema, bad):
    with pytest.raises(ValidationError):
        gen.parse_command(bad)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, command_schema)


def test_non_object_rejected(gen):
    for bad in (None, "place", 7, [], [{"type": "undo"}]):
        with pytest.raises(ValidationError):
            gen.parse_command(bad)


def _required_cases():
    schema = json.loads((SCHEMA_DIR / "command.schema.json").read_text(encoding="utf-8"))
    cases = []
    for t, example in EXAMPLES:
        defn = schema["definitions"][pascal(t) + "Command"]
        for field in defn.get("required", []):
            if field != "type":
                cases.append((t, field, example))
    return cases


REQUIRED = _required_cases()


@pytest.mark.parametrize(
    ("ctype", "field", "example"), REQUIRED, ids=[f"{t}.{f}" for t, f, _ in REQUIRED]
)
def test_missing_required_field_rejected(gen, command_schema, ctype, field, example):
    broken = copy.deepcopy(example)
    del broken[field]
    with pytest.raises(ValidationError):
        gen.parse_command(broken)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, command_schema)


@pytest.mark.parametrize(("ctype", "example"), EXAMPLES, ids=_ids(EXAMPLES))
def test_unknown_extra_field_rejected(gen, ctype, example):
    broken = {**example, "bogusField": 1}
    with pytest.raises(ValidationError):
        gen.parse_command(broken)


def test_specific_bad_values_rejected(gen):
    base_place = {"type": "place", "kind": "blow", "x": 1, "y": 2, "rot": 0}
    gen.parse_command(base_place)
    for patch in (
        {"kind": "not_a_building"},
        {"rot": 4},
        {"rot": "1"},
        {"x": "left"},
        {"level": 4},
        {"config": {"fluid": "water"}},
    ):
        with pytest.raises(ValidationError):
            gen.parse_command({**base_place, **patch})
    with pytest.raises(ValidationError):
        gen.parse_command({"type": "expandHangar", "side": "UP"})
    with pytest.raises(ValidationError):
        gen.parse_command({"type": "paint", "cells": [{"x": 1, "y": 1}], "color": "blue"})
    with pytest.raises(ValidationError):
        gen.parse_command({"type": "sellMarket", "sku": "beer", "boxes": 1})
    with pytest.raises(ValidationError):
        gen.parse_command({"type": "placeConveyor", "kind": "belt", "path": [{"x": 1, "y": 1}]})


def test_submit_shift_result_shape(gen):
    base = {
        "type": "submitShift",
        "buildingId": "b1",
        "result": {
            "gameId": "g", "profession": "kvasovar", "result": "win", "score": 0.5,
            "durationMs": 1000, "difficulty": 0.2, "metrics": {"a": 1},
        },
    }  # fmt: skip
    gen.parse_command(base)
    for patch in ({"profession": "wizard"}, {"result": "draw"}, {"metrics": {"a": "x"}}):
        broken = copy.deepcopy(base)
        broken["result"].update(patch)
        with pytest.raises(ValidationError):
            gen.parse_command(broken)

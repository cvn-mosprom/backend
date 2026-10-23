"""Shared fixtures for the contract tests.

The generated package lives at api/app/contract/generated (it uses relative imports), so it is loaded
by file path under the private name ``contract_generated``: no dependency on the rest of the api package
(FastAPI, SQLAlchemy, ...) and no sys.path pollution.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

CONTRACT_DIR = Path(__file__).resolve().parents[1]
ROOT = CONTRACT_DIR.parent
GENERATED_DIR = ROOT / "api" / "app" / "contract" / "generated"
SCHEMA_DIR = CONTRACT_DIR / "schema"
EXAMPLES_DIR = CONTRACT_DIR / "examples"
TYPES_TS = ROOT / "vendor" / "frontend" / "web" / "src" / "lib" / "api" / "types.ts"


def _load_generated():
    name = "contract_generated"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, GENERATED_DIR / "__init__.py", submodule_search_locations=[str(GENERATED_DIR)]
    )
    assert spec and spec.loader, f"cannot load {GENERATED_DIR}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def gen():
    """The generated package (Command, GameSnapshot, CommandResult, COMMAND_TYPES, parse_command)."""
    return _load_generated()


@pytest.fixture(scope="session")
def command_schema() -> dict:
    return json.loads((SCHEMA_DIR / "command.schema.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def snapshot_schema() -> dict:
    return json.loads((SCHEMA_DIR / "snapshot.schema.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def command_examples() -> dict[str, list[dict]]:
    return json.loads((EXAMPLES_DIR / "commands.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def snapshot_example() -> dict:
    return json.loads((EXAMPLES_DIR / "snapshot.json").read_text(encoding="utf-8"))


def pascal(s: str) -> str:
    return s[:1].upper() + s[1:]


def all_command_examples() -> list[tuple[str, dict]]:
    """(type, example) pairs for parametrization (evaluated at collection time)."""
    data = json.loads((EXAMPLES_DIR / "commands.json").read_text(encoding="utf-8"))
    return [(t, ex) for t, items in data.items() for ex in items]

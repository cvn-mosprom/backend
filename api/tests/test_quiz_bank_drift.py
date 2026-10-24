"""S9 drift tests: ONE quiz bank and one department list for client and server.

The server bank (app/data/quiz_bank.json) is generated from vendor/frontend/web/src/lib/api/mock/quiz.ts by
scripts/gen_quiz_bank.py; these tests fail when the vendored client moves on and the JSON does not (regenerate with
`python scripts/gen_quiz_bank.py`). Skipped only when the submodule is not checked out."""
from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from app import leads as leads_mod
from app import quiz_bank

ROOT = Path(__file__).resolve().parents[2]
VENDOR_WEB = ROOT / "vendor" / "frontend" / "web" / "src" / "lib"
QUIZ_TS = VENDOR_WEB / "api" / "mock" / "quiz.ts"
LEAD_TS = VENDOR_WEB / "ui" / "lead.ts"
BOT_FORMAT = ROOT / "bot" / "app" / "hr" / "format.py"

_spec = importlib.util.spec_from_file_location("gen_quiz_bank", ROOT / "scripts" / "gen_quiz_bank.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)  # type: ignore[union-attr]

needs_vendor = pytest.mark.skipif(not QUIZ_TS.exists(), reason="vendor/frontend submodule is not checked out")


@needs_vendor
def test_server_bank_is_the_vendored_client_bank():
    vendor = gen.parse_bank(QUIZ_TS.read_text(encoding="utf-8"))
    assert len(vendor) == quiz_bank.TOTAL == 8
    for v, q in zip(vendor, quiz_bank.QUESTIONS, strict=True):
        assert (q.id, q.fact_id, q.text) == (v["id"], v["factId"], v["text"])
        assert list(q.option_ids) == [o["id"] for o in v["options"]]
        assert list(q.options) == [o["text"] for o in v["options"]]
        assert q.correct_id == v["correct"]


@needs_vendor
def test_generated_json_is_current():
    """Byte for byte what `python scripts/gen_quiz_bank.py` would write."""
    expected = gen.render(gen.parse_bank(QUIZ_TS.read_text(encoding="utf-8")))
    assert quiz_bank.BANK_FILE.read_text(encoding="utf-8").replace("\r\n", "\n") == expected, \
        "api/app/data/quiz_bank.json is stale: run python scripts/gen_quiz_bank.py"


@pytest.mark.skipif(not LEAD_TS.exists(), reason="vendor/frontend submodule is not checked out")
def test_departments_are_the_clients_ids_and_labels():
    ts = LEAD_TS.read_text(encoding="utf-8")
    block = re.search(r"export const DEPARTMENTS[^=]*=\s*\[(.*?)\];", ts, re.S)
    assert block, "DEPARTMENTS not found in lead.ts"
    client = dict(re.findall(r"\['(\w+)',\s*'([^']+)'\]", block.group(1)))
    assert client and {k: v[0] for k, v in leads_mod.DEPARTMENTS.items()} == client


@pytest.mark.skipif(not LEAD_TS.exists(), reason="vendor/frontend submodule is not checked out")
def test_lead_enums_match_the_client():
    ts = (VENDOR_WEB / "api" / "types.ts").read_text(encoding="utf-8")
    kind = re.search(r"institutionKind\?:\s*([^;]+);", ts)
    assert kind and set(re.findall(r"'(\w+)'", kind.group(1))) == set(leads_mod.INSTITUTION_KINDS)
    practice = re.search(r"practiceType:\s*([^;]+);", ts)
    assert practice and set(re.findall(r"'(\w+)'", practice.group(1))) == {"paid", "free", "job"}


def test_bot_knows_the_same_bank():
    """The bot cannot import the api: its copy of the bank ids / size must be the server's (HR stats, CSV, card)."""
    tree = ast.parse(BOT_FORMAT.read_text(encoding="utf-8"))
    consts = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and t.id in ("QUIZ_TOTAL", "QUIZ_QUESTION_IDS")}
    assert consts["QUIZ_TOTAL"] == quiz_bank.TOTAL
    assert tuple(consts["QUIZ_QUESTION_IDS"]) == quiz_bank.IDS


# ---------------------------------------------------------------- the generator's parser

HELPER = "options: opts.map((t, i) => ({ id: 'abc'[i], text: t }))"


def ts(body: str, helper: str = HELPER) -> str:
    return f"const q = (...opts) => ({{ {helper} }});\nexport const QUIZ_QUESTIONS: QuizQuestion[] = [\n{body}\n];\n"


def test_parser_handles_comments_quotes_and_escapes():
    bank = gen.parse_bank(ts("""
	// a comment, with a ) and a ' in it
	q('q1', 'f1', 'Why \\'this\\'?', 'b', "one", 'two (2)', `thr"ee`), /* inline ) */
	q('q2', 'f2', 'Юникод \\u00ab\\u00bb', 'a', 'x', 'y')
"""))
    assert [q["id"] for q in bank] == ["q1", "q2"]
    assert bank[0]["text"] == "Why 'this'?" and [o["text"] for o in bank[0]["options"]] == ["one", "two (2)", 'thr"ee']
    assert bank[1]["text"] == "Юникод «»" and bank[1]["correct"] == "a" and [o["id"] for o in bank[1]["options"]] == ["a", "b"]


@pytest.mark.parametrize("src,why", [
    ("export const QUIZ_QUESTIONS: QuizQuestion[] = [];", "empty bank"),
    (ts("q('q1', 'f', 'Q?', 'd', 'x', 'y')"), "correct answer outside the options"),
    (ts("q('q1', 'f', 'Q?', 'a', 'x', 'y'), q('q1', 'f', 'Q?', 'a', 'x', 'y')"), "duplicate ids"),
    (ts("q('q1', 'f', 'Q?', 'a', 'x', 'y', 'z', 'w')"), "four options"),
    (ts("q('q1', 'f', 'Q?', 'a')"), "no options"),
    (ts("q('q1', 'f', `Q ${x}?`, 'a', 'x', 'y')"), "template with interpolation"),
    (ts("q('q1', 'f', TEXT, 'a', 'x', 'y')"), "an identifier instead of a string"),
    (ts("{ id: 'q1', text: 'Q?' }"), "an object literal instead of q(...)"),
    (ts("q('q1', 'f', 'Q?', 'a', 'x', 'y')", helper="options: opts.map((t, i) => ({ id: String(i), text: t }))"),
     "the helper no longer builds a/b/c ids"),
    ("const x = 1;", "no QUIZ_QUESTIONS"),
])
def test_parser_fails_loudly_on_anything_unexpected(src, why):
    with pytest.raises(gen.BankParseError):
        gen.parse_bank(src)


def test_parser_reads_the_real_file_to_8_questions_if_present():
    if not QUIZ_TS.exists():
        pytest.skip("vendor/frontend submodule is not checked out")
    bank = gen.parse_bank(QUIZ_TS.read_text(encoding="utf-8"))
    assert [q["id"] for q in bank] == ["q_year", "q_olymp", "q_kvas", "q_izba", "q_white", "q_cities", "q_fields", "q_space"]


@pytest.mark.skipif(not (VENDOR_WEB / "content" / "buildings.ts").exists(), reason="vendor/frontend submodule is not checked out")
def test_vacancies_are_the_clients_profession_ids():
    ts = (VENDOR_WEB / "content" / "buildings.ts").read_text(encoding="utf-8")
    block = re.search(r"export type ProfessionId =([^;]+);", ts)
    assert block and set(re.findall(r"'(\w+)'", block.group(1))) == set(leads_mod.VACANCIES)

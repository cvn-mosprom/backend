#!/usr/bin/env python3
"""Generate api/app/data/quiz_bank.json from the frontend question bank (vendor/frontend/.../api/mock/quiz.ts).

One bank for the whole product: the client renders QUIZ_QUESTIONS and sends the option id ('a' | 'b' | 'c'); the server
checks against the same bank. The api image builds from ./api (no vendor/ in the context), so the bank is generated at
dev time and committed; api/tests/test_quiz_bank_drift.py fails when the vendored TS and the JSON diverge.

    python scripts/gen_quiz_bank.py            # rewrite the JSON
    python scripts/gen_quiz_bank.py --check    # exit 1 when the JSON is stale (CI / pre-commit)

The parser is deliberately strict: it understands the `q(id, factId, text, correct, ...options)` helper used by the
file today (option ids are 'abc'[i]) and fails loudly on anything else, so a restructured bank never drifts silently.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "vendor" / "frontend" / "web" / "src" / "lib" / "api" / "mock" / "quiz.ts"
OUT = ROOT / "api" / "app" / "data" / "quiz_bank.json"

_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


class BankParseError(ValueError):
    pass


def _skip_ws_and_comments(s: str, i: int) -> int:
    while i < len(s):
        if s[i].isspace():
            i += 1
        elif s.startswith("//", i):
            i = s.find("\n", i)
            i = len(s) if i < 0 else i
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            if j < 0:
                raise BankParseError("unterminated block comment")
            i = j + 2
        else:
            break
    return i


def _string(s: str, i: int) -> tuple[str, int]:
    """TS string literal at s[i] ('..', "..", or a `..` without ${}), returns (value, index after it)."""
    quote = s[i]
    if quote not in "'\"`":
        raise BankParseError(f"expected a string literal at offset {i}: {s[i:i + 30]!r}")
    out: list[str] = []
    i += 1
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            nxt = s[i + 1]
            if nxt == "u":
                if s[i + 2] == "{":
                    j = s.index("}", i)
                    out.append(chr(int(s[i + 3:j], 16)))
                    i = j + 1
                else:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                continue
            if nxt == "x":
                out.append(chr(int(s[i + 2:i + 4], 16)))
                i += 4
                continue
            out.append(_ESC.get(nxt, nxt))
            i += 2
            continue
        if ch == quote:
            return "".join(out), i + 1
        if quote == "`" and s.startswith("${", i):
            raise BankParseError("template literals with ${} are not supported")
        if ch == "\n" and quote != "`":
            raise BankParseError("newline inside a string literal")
        out.append(ch)
        i += 1
    raise BankParseError("unterminated string literal")


def _call_args(s: str, i: int) -> tuple[list[str], int]:
    """`q(` at s[i-1]: the string-literal arguments up to the closing parenthesis."""
    args: list[str] = []
    while True:
        i = _skip_ws_and_comments(s, i)
        if s[i] == ")":
            return args, i + 1
        val, i = _string(s, i)
        args.append(val)
        i = _skip_ws_and_comments(s, i)
        if s[i] == ",":
            i += 1
        elif s[i] != ")":
            raise BankParseError(f"expected ',' or ')' at offset {i}: {s[i:i + 30]!r}")


def parse_bank(ts: str) -> list[dict[str, Any]]:
    if not re.search(r"options:\s*opts\.map\(\(t,\s*i\)\s*=>\s*\(\{\s*id:\s*'abc'\[i\]", ts):
        raise BankParseError("the `q(...)` helper no longer builds option ids as 'abc'[i]: update scripts/gen_quiz_bank.py")
    m = re.search(r"export\s+const\s+QUIZ_QUESTIONS\s*:\s*QuizQuestion\[\]\s*=\s*\[", ts)
    if not m:
        raise BankParseError("`export const QUIZ_QUESTIONS: QuizQuestion[] = [` not found")
    i, out = m.end(), []
    while True:
        i = _skip_ws_and_comments(ts, i)
        if ts[i] == "]":
            break
        if not ts.startswith("q(", i):
            raise BankParseError(f"expected a q(...) call at offset {i}: {ts[i:i + 40]!r}")
        args, i = _call_args(ts, i + 2)
        if len(args) < 6:
            raise BankParseError(f"q(...) needs id, factId, text, correct and >= 2 options, got {args!r}")
        qid, fact_id, text, correct, *opts = args
        ids = ["abc"[k] for k in range(len(opts))] if len(opts) <= 3 else None
        if ids is None:
            raise BankParseError(f"{qid}: more than 3 options (the client helper only has a, b, c)")
        if correct not in ids:
            raise BankParseError(f"{qid}: correct={correct!r} is not one of {ids}")
        out.append({"id": qid, "factId": fact_id, "text": text,
                    "options": [{"id": oid, "text": t} for oid, t in zip(ids, opts)], "correct": correct})
        i = _skip_ws_and_comments(ts, i)
        if ts[i] == ",":
            i += 1
    if not out:
        raise BankParseError("empty bank")
    if len({q["id"] for q in out}) != len(out):
        raise BankParseError("duplicate question ids")
    return out


def render(bank: list[dict[str, Any]]) -> str:
    return json.dumps(bank, ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str]) -> int:
    bank = parse_bank(SRC.read_text(encoding="utf-8"))
    text = render(bank)
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/gen_quiz_bank.py", file=sys.stderr)
            return 1
        print(f"ok: {len(bank)} questions")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(bank)} questions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Unexpected skip/xfail and stale allowlist entries fail CI."""
import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--allowlist", type=Path)
    args = parser.parse_args()
    exceptions = json.loads(args.allowlist.read_text()) if args.allowlist else {}
    seen = set()
    errors = []
    for case in ET.parse(args.report).iter("testcase"):
        skipped = case.find("skipped")
        if skipped is None:
            continue
        key = f'{case.get("classname")}::{case.get("name")}'
        seen.add(key)
        item = exceptions.get(key)
        if not item or not all(item.get(field) for field in ("owner", "reason", "expires")):
            errors.append(key)
        elif item["expires"] < __import__("datetime").date.today().isoformat():
            errors.append(f"expired exception: {key}")
    errors.extend(f"stale exception: {key}" for key in exceptions if key not in seen)
    if errors:
        raise SystemExit("Unexpected test skips / stale exceptions:\n" + "\n".join(errors))


if __name__ == "__main__":
    main()

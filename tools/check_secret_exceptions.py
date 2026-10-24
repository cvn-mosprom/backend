#!/usr/bin/env python3
"""Only exact reviewed secret-scanner false positives may be exempted, until expiry."""
import datetime
import re
from pathlib import Path

path = Path('.gitleaksignore')
text = path.read_text() if path.exists() else ''
entries = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
if entries:
    expiry = re.search(r'Review expires: (\d{4}-\d{2}-\d{2})', text)
    if not expiry or datetime.date.fromisoformat(expiry[1]) < datetime.date.today():
        raise SystemExit('secret-scanner exceptions expired or missing expiry')
    if 'Owner:' not in text or 'reason:' not in text:
        raise SystemExit('secret-scanner exceptions need an owner and a reason')
    if any(not re.fullmatch(r'[0-9a-f]{40}:[^*\n]+:[a-z0-9-]+:[0-9]+', item) for item in entries):
        raise SystemExit('only exact finding fingerprints may be excepted')

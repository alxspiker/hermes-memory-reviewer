#!/usr/bin/env python3
"""Cron monitor: emit a stable fingerprint of pending memory proposals, no secrets.

Empty and unchanged queues use no model tokens after the baseline; when proposal content
changes the existing Hermes monitor infrastructure triggers one agent run.
"""
import hashlib
import json
import os
from pathlib import Path

home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
pending = home / "pending" / "memory"
items = []
for path in sorted(pending.glob("*.json")):
    if not path.is_file() or path.is_symlink():
        continue
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("id") == path.stem:
            items.append((data["id"], hashlib.sha256(raw).hexdigest()[:16]))
    except (OSError, ValueError, KeyError):
        # A malformed entry is reported by the reviewer tool; don't print its payload.
        items.append((path.stem, "invalid"))
if items:
    print("pending=" + json.dumps(items, separators=(",", ":")))
else:
    print("pending=none")

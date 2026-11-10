"""Native Hermes tool for one authorized, Grok-pinned memory review cron job.

Uses Hermes's own pending store and MemoryStore (including matched-entry pinning,
atomic batch validation and char budgets). Model text is never trusted as a path.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from hermes_constants import get_hermes_home

JOB_NAME = "pending-memory-grok-reviewer"
ID_RE = re.compile(r"^[a-f0-9]{8}$")
ALLOWED = {"add", "remove", "patch"}
# A remote LLM must not be allowed to silently erase hard safety/authorization rules.
HUMAN_ONLY = ("explicit user approval", "without explicit user ok", "never touch xp")

SCHEMA = {
    "name": "memory_pending_review",
    "description": (
        "For the authorized Grok memory-review worker only: inspect the current pending "
        "Hermes memory proposals, revise individual proposals, approve exact pinned edits, "
        "or reject them. Every action requires a substantive reason and is audited. "
        "Always inspect each proposal and current memory first. Do not auto-approve "
        "unrelated patches or removal of important instructions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "inspect", "revise", "approve", "reject"]},
            "id": {"type": "string", "description": "Exact 8-character pending memory ID, except for list."},
            "reason": {"type": "string", "description": "Why the proposed decision preserves useful information and is justified."},
            "operations": {
                "type": "array", "description": "For revise only: complete replacement list of memory operations, max 6. "
                "Each entry is add/patch/remove with content and/or old_text. "
                "Target is fixed by the proposal and cannot be changed.",
                "items": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": ["add", "patch", "remove"]},
                    "content": {"type": "string"}, "old_text": {"type": "string"}
                }, "required": ["action"]},
            },
        },
        "required": ["action"],
    },
}


def _result(**kwargs):
    return json.dumps(kwargs, ensure_ascii=False, default=str)


def _authorized():
    from cron.execution_identity import current_cron_execution

    execution = current_cron_execution()
    return execution is not None and execution.job_name == JOB_NAME


def _state_dir():
    home = get_hermes_home()
    state = home / "memory-reviewer"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    return state


@contextmanager
def _mutex():
    path = _state_dir() / ".lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _audit(pending_id, action, reason, outcome, **extra):
    line = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pending_id": pending_id, "action": action, "reason": reason,
            "outcome": outcome, **extra}
    path = _state_dir() / "audit.jsonl"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write(json.dumps(line, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())


def _backup(record):
    """Before changes, retain exact proposal + current backing memory; do not proceed on failure."""
    pending_id = record["id"]
    target = record["payload"]["target"]
    home = get_hermes_home()
    folder = home / "backups" / "memory-reviewer" / (pending_id + "-" + str(time.time_ns()))
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    original = folder / "proposal.json"
    original.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    original.chmod(0o600)
    source = home / "memories" / ("MEMORY.md" if target == "memory" else "USER.md")
    if source.exists():
        saved = folder / source.name
        shutil.copy2(source, saved)
        saved.chmod(0o600)
    return str(folder)


def _protected(payload):
    """Fail closed on unattended destructive edits to standing permission boundaries."""
    ops = payload.get("operations") if payload.get("action") == "batch" else [payload]
    for op in ops or []:
        if not isinstance(op, dict) or op.get("action") not in ("patch", "remove"):
            continue
        entry = str(op.get("matched_entry") or "").lower()
        if any(s in entry for s in HUMAN_ONLY):
            return True
    return False


def _safe_id(args):
    value = args.get("id")
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else None


def _explain(args):
    value = args.get("reason")
    return value.strip() if isinstance(value, str) and 15 <= len(value.strip()) <= 1300 else None


def _list(wa):
    items = []
    for record in wa.list_pending(wa.MEMORY)[:100]:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        items.append({"id": record.get("id"), "created_at": record.get("created_at"),
                      "origin": record.get("origin"), "target": payload.get("target"),
                      "summary": str(record.get("summary") or "")[:350],
                      "revision_count": record.get("revision_count", 0)})
    return _result(success=True, pending=items, count=len(items))


def _inspect(record, store):
    payload = record.get("payload") or {}
    target = payload.get("target")
    if target not in ("memory", "user"):
        return _result(success=False, error="Invalid target in pending proposal.")
    return _result(success=True, pending=record,
                   current_entries=store._entries_for(target),
                   memory_usage=store._usage(target),
                   human_only_protected=_protected(payload))


def _revise(record, args, store, wa, reason):
    original = record.get("payload") or {}
    target = original.get("target")
    incoming = args.get("operations")
    if not isinstance(incoming, list) or not 1 <= len(incoming) <= 6:
        return _result(success=False, error="Provide 1-6 revised operations.")
    cleaned = []
    for op in incoming:
        if not isinstance(op, dict) or op.get("action") not in ALLOWED:
            return _result(success=False, error="Each operation needs add, patch or remove.")
        kind = op["action"]
        content = op.get("content") or ""
        old_text = op.get("old_text") or ""
        if not isinstance(content, str) or not isinstance(old_text, str):
            return _result(success=False, error="Operation content/old_text must be strings.")
        if len(content) > 1800 or len(old_text) > 2200:
            return _result(success=False, error="Operation text exceeds bounded limits.")
        if kind in ("add", "patch") and not content.strip():
            return _result(success=False, error="Add/patch requires content.")
        if kind in ("patch", "remove") and not old_text.strip():
            return _result(success=False, error="Patch/remove requires old_text.")
        cleaned.append({"action": kind, **({"content": content} if content else {}),
                        **({"old_text": old_text} if old_text else {})})
    # Revisions cannot move destructive edits onto arbitrary other entries; the
    # only previously authorized targets are the original proposal's pins.
    original_ops = original.get("operations") if original.get("action") == "batch" else [original]
    original_targets = {op.get("matched_entry") for op in original_ops or [] if isinstance(op, dict)
                        and op.get("action") in ("patch", "remove") and op.get("matched_entry")}
    verdict = store.resolve_batch_entries(target, cleaned)
    if not verdict.get("success"):
        return _result(success=False, error="Revised batch fails dry-run", details=verdict)
    pins = verdict.get("matched_entries") or []
    for op, pin in zip(cleaned, pins):
        if op["action"] in ("patch", "remove"):
            if not pin or pin not in original_targets:
                return _result(success=False, error="Revision cannot introduce a destructive target not pinned in the original proposal.")
            op["matched_entry"] = pin
    new_payload = {"action": "batch", "target": target, "operations": cleaned}
    if _protected(new_payload):
        return _result(success=False, error="This touches a standing permission boundary and requires human review.")
    backup = _backup(record)
    new_record = dict(record)
    new_record["payload"] = new_payload
    new_record["summary"] = "Grok-reviewed revision: " + reason[:150]
    new_record["revision_count"] = int(record.get("revision_count") or 0) + 1
    new_record["revised_at"] = time.time()
    new_record["review_reason"] = reason
    from utils import atomic_json_write
    _audit(record["id"], "revise", reason, "intent", backup=backup)
    atomic_json_write(wa._pending_path(wa.MEMORY, record["id"]), new_record)
    _audit(record["id"], "revise", reason, "success", backup=backup)
    return _result(success=True, id=record["id"], updated_payload=new_payload,
                   instruction="Inspect the revised proposal, then approve or reject explicitly.")


def handle(args, **_):
    try:
        if not _authorized():
            return _result(success=False, error="This tool is restricted to the authorized Grok memory-review cron job.")
        if not isinstance(args, dict):
            return _result(success=False, error="Expected a JSON argument object.")
        from tools import write_approval as wa
        from tools.memory_tool import load_on_disk_store, apply_memory_pending

        action = args.get("action")
        if action == "list":
            return _list(wa)
        pending_id = _safe_id(args)
        if not pending_id:
            return _result(success=False, error="Specify a valid pending ID from list.")
        with _mutex():
            record = wa.get_pending(wa.MEMORY, pending_id)
            if record is None:
                return _result(success=False, error="Pending record no longer exists; do not retry without listing.")
            if not isinstance(record.get("payload"), dict) or record["payload"].get("target") not in ("memory", "user"):
                return _result(success=False, error="Invalid pending payload; human review required.")
            store = load_on_disk_store()
            if action == "inspect":
                return _inspect(record, store)
            if action not in ("revise", "approve", "reject"):
                return _result(success=False, error="Unknown action; use list, inspect, revise, approve, reject.")
            reason = _explain(args)
            if not reason:
                return _result(success=False, error="Give a substantive 15-1300-character reason for the decision.")
            if action == "revise":
                return _revise(record, args, store, wa, reason)
            if action == "approve" and _protected(record["payload"]):
                return _result(success=False, error="Protected permission-related memory requires human approval; do not approve or rewrite it automatically.")
            backup = _backup(record)
            _audit(pending_id, action, reason, "intent", backup=backup)
            if action == "approve":
                result = apply_memory_pending(record["payload"], store)
                if not result.get("success"):
                    _audit(pending_id, action, reason, "failed", error=str(result.get("error"))[:400])
                    return _result(success=False, id=pending_id, error=result.get("error"), pending_retained=True)
            if not wa.discard_pending(wa.MEMORY, pending_id):
                _audit(pending_id, action, reason, "pending_removal_failed")
                return _result(success=False, id=pending_id, error="Failed to clear pending record", backup=backup)
            _audit(pending_id, action, reason, "success", backup=backup)
            return _result(success=True, id=pending_id, decision=action,
                           applied=(action == "approve"), backup=backup)
    except Exception as exc:
        return _result(success=False, error=f"Review tool failed closed: {type(exc).__name__}: {exc}")


def register(ctx):
    """Register with the supported Hermes plugin API; no host monkeypatching."""
    ctx.register_tool(name="memory_pending_review", toolset="memory-reviewer",
                      schema=SCHEMA, handler=handle,
                      description="Inspect, safely revise, approve or reject staged memory writes",
                      emoji="🧠")

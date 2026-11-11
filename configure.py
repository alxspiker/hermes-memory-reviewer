#!/usr/bin/env python3
"""Interactive model selection and safe, repeatable Hermes memory reviewer setup."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

JOB_NAME = "pending-memory-reviewer"
LEGACY_NAME = "pending-memory-grok-reviewer"
JOB_NAMES = (JOB_NAME, LEGACY_NAME)
TOOLSETS = ["memory-reviewer", "no_mcp"]
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@+\-]{1,200}$")
COMMON = ("nous", "openrouter", "openai", "google", "xai-oauth", "ollama", "mistral")


def hermes(*args, capture=False):
    p = subprocess.run(["hermes", *args], check=True, text=True,
                       stdout=subprocess.PIPE if capture else None)
    return p.stdout.strip() if capture else ""


def config(key):
    try:
        return json.loads(hermes("config", "get", key, "--json", capture=True))
    except (subprocess.CalledProcessError, ValueError):
        return None


def jobs(home):
    path = home / "cron" / "jobs.json"
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        raw = json.load(f)
    rows = raw.get("jobs", []) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise RuntimeError("Unknown cron jobs format; refusing to alter it")
    return rows


def current_job(home):
    matches = [j for j in jobs(home)
               if isinstance(j, dict) and j.get("name") in JOB_NAMES]
    if len(matches) > 1:
        raise RuntimeError("Multiple reviewer jobs found; resolve duplicates manually")
    if matches and not matches[0].get("id"):
        raise RuntimeError("Existing reviewer job lacks an ID")
    return matches[0] if matches else None


def validate(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {name}; supply a Hermes ID without whitespace or flags")
    return value


def menu(label, options):
    print(f"\n{label} choices (not a live API availability check):")
    for n, (value, source) in enumerate(options, 1):
        print(f"  {n}. {value} — {source}")
    print(f"  {len(options)+1}. Enter another {label.lower()} ID")
    while True:
        answer = input("Choose [1]: ").strip() or "1"
        if answer.isdecimal() and 1 <= int(answer) <= len(options):
            return options[int(answer)-1][0]
        if answer == str(len(options)+1):
            return input(f"{label} ID: ").strip()
        if IDENTIFIER.fullmatch(answer):
            return answer
        print("Enter a number or an exact ID.")


def selection(existing, args):
    configured_model = config("model")
    configured_model = configured_model if isinstance(configured_model, dict) else {}
    configured_providers = config("providers")
    configured_providers = configured_providers if isinstance(configured_providers, dict) else {}
    providers = []

    def add(items, value, reason):
        if isinstance(value, str) and IDENTIFIER.fullmatch(value) and value not in [v for v, _ in items]:
            items.append((value, reason))

    if existing:
        add(providers, existing.get("provider") or existing.get("model_provider"), "current reviewer")
    add(providers, configured_model.get("provider"), "Hermes default")
    for provider in sorted(configured_providers):
        add(providers, provider, "configured provider")
    for provider in COMMON:
        add(providers, provider, "common provider (credentials not checked)")

    provider = args.provider or os.getenv("HERMES_REVIEW_PROVIDER")
    interactive = sys.stdin.isatty() and not args.non_interactive
    if not provider:
        provider = menu("Provider", providers) if interactive else (
            providers[0][0] if providers else None)
    provider = validate(provider, "provider")

    models = []
    if existing and (existing.get("provider") or existing.get("model_provider")) == provider:
        add(models, existing.get("model"), "current reviewer")
    if configured_model.get("provider") == provider:
        add(models, configured_model.get("default"), "Hermes default")
    entry = configured_providers.get(provider)
    if isinstance(entry, dict):
        add(models, entry.get("model") or entry.get("default_model"), "configured provider")

    model = args.model or os.getenv("HERMES_REVIEW_MODEL")
    if not model:
        model = menu("Model", models) if interactive else (models[0][0] if models else None)
    model = validate(model, "model")
    return provider, model


@contextmanager
def setup_lock(home):
    state = home / "memory-reviewer"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / ".setup.lock").open("a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def monitor_wrapper(home):
    path = home / "scripts" / "memory-pending-monitor.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = """#!/usr/bin/env python3
import os
import runpy
from pathlib import Path
home = Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes')
runpy.run_path(str(home / 'plugins' / 'memory-reviewer' / 'monitor.py'), run_name='__main__')
"""
    fd, tmp = tempfile.mkstemp(prefix=".memory-monitor-", dir=path.parent)
    try:
        os.fchmod(fd, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def secure_job(home, identifier):
    cron = home / "cron"
    path = cron / "jobs.json"
    # Match the real cron writer's flock; reread under the lock to avoid lost writes.
    with (cron / ".jobs.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with path.open(encoding="utf-8-sig") as f:
            raw = json.load(f)
        rows = raw["jobs"] if isinstance(raw, dict) else raw
        matches = [j for j in rows if j.get("id") == identifier and j.get("name") == JOB_NAME]
        if len(matches) != 1:
            raise RuntimeError("Could not uniquely verify the reviewer job's identity")
        matches[0]["enabled_toolsets"] = TOOLSETS
        fd, tmp = tempfile.mkstemp(prefix=".jobs-reviewer-", dir=cron)
        try:
            os.fchmod(fd, stat.S_IMODE(path.stat().st_mode))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def run(args):
    home = Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")
    plugin = home / "plugins" / "memory-reviewer"
    if not (plugin / "monitor.py").is_file() or not (plugin / "review_prompt.txt").is_file():
        raise RuntimeError(f"Install the memory-reviewer plugin at {plugin} first")
    if not shutil.which("hermes"):
        raise RuntimeError("Hermes must be on PATH")
    with setup_lock(home):
        existing = current_job(home)
        provider, model = selection(existing, args)
        timezone = args.timezone or os.getenv("HERMES_REVIEW_TIMEZONE")
        if timezone and (not IDENTIFIER.fullmatch(timezone) or ":" in timezone):
            raise ValueError("Timezone must be an IANA name such as America/Edmonton")
        if sys.stdin.isatty() and not args.non_interactive:
            print(f"\nReviewer: {provider} / {model} — Hermes' main model is unchanged.")
            if input("Apply configuration? [Y/n]: ").strip().lower() not in ("", "y", "yes"):
                print("Cancelled; nothing changed.")
                return
        wrapper = monitor_wrapper(home)
        hermes("plugins", "enable", "memory-reviewer")
        hermes("config", "set", "memory.write_approval", "true")
        prompt = (plugin / "review_prompt.txt").read_text(encoding="utf-8")
        job_id = None
        was_active = False
        updated = False
        try:
            if existing:
                job_id = str(existing["id"])
                was_active = bool(existing.get("enabled", True))
                if was_active:
                    hermes("cron", "pause", job_id)
                edit = ["cron", "edit", job_id, "--name", JOB_NAME, "--provider", provider,
                        "--model", model, "--prompt", prompt, "--monitor-script", str(wrapper),
                        "--reasoning-effort", "low"]
                if timezone:
                    edit += ["--timezone", timezone]
                hermes(*edit)
            else:
                create = ["cron", "create", "* * * * *", prompt,
                          "--name", JOB_NAME, "--provider", provider, "--model", model,
                          "--reasoning-effort", "low", "--timezone", timezone or "UTC",
                          "--monitor-script", str(wrapper), "--deliver", "local", "--paused"]
                hermes(*create)
                found = current_job(home)
                if not found or found.get("name") != JOB_NAME:
                    raise RuntimeError("Created cron job could not be verified")
                job_id = str(found["id"])
                was_active = True
            secure_job(home, job_id)
            updated = True
        finally:
            # Existing paused jobs stay paused; active ones resume even after
            # an edit error. New jobs start paused until permissions are applied.
            if was_active and job_id:
                hermes("cron", "resume", job_id)
        if updated:
            print(f"\nConfigured {JOB_NAME} ({job_id}): {provider} / {model}")
            print("Rerun setup.sh to change the model later, without reinstalling.")
            print("Remote model availability is verified by Hermes when the job runs.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Configure the autonomous Hermes memory reviewer")
    parser.add_argument("--configure", action="store_true", help="Reconfigure an existing reviewer job (default)")
    parser.add_argument("--provider", help="Hermes provider ID")
    parser.add_argument("--model", help="Exact model ID")
    parser.add_argument("--timezone", help="Timezone for new jobs, or override on existing jobs")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Use arguments, environment variables, or previous settings")
    args = parser.parse_args(argv)
    try:
        run(args)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"Memory reviewer setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

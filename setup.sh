#!/usr/bin/env bash
# One-time scheduler wiring for Hermes Memory Reviewer (run explicitly by owner).
set -euo pipefail

: "${HERMES_HOME:=$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/memory-reviewer"
MONITOR="$HERMES_HOME/scripts/memory-pending-monitor.py"
JOB_NAME="pending-memory-grok-reviewer"
PROVIDER="${HERMES_REVIEW_PROVIDER:-xai-oauth}"
MODEL="${HERMES_REVIEW_MODEL:-grok-4.20-0309-reasoning}"
TZ_NAME="${HERMES_REVIEW_TIMEZONE:-UTC}"

command -v hermes >/dev/null || { echo 'Install Hermes and add it to PATH first.' >&2; exit 1; }
test -f "$PLUGIN_DIR/monitor.py" || { echo "Install the plugin in $PLUGIN_DIR first." >&2; exit 1; }

mkdir -p "$HERMES_HOME/scripts"
# Stable wrapper: update the Git plugin and its monitor logic updates automatically.
cat > "$MONITOR" <<'PY'
#!/usr/bin/env python3
import os
import runpy
from pathlib import Path
home = Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes')
runpy.run_path(str(home / 'plugins' / 'memory-reviewer' / 'monitor.py'), run_name='__main__')
PY
chmod 700 "$MONITOR"

hermes plugins enable memory-reviewer
hermes config set memory.write_approval true

# Native creation is initially paused: patch the allowlist before the first run.
if ! python3 - "$HERMES_HOME" "$JOB_NAME" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])/'cron'/'jobs.json'
records=json.load(open(p)).get('jobs',[]) if p.exists() else []
raise SystemExit(0 if any(j.get('name')==sys.argv[2] for j in records) else 1)
PY
then
  hermes cron create '* * * * *' "$(cat "$PLUGIN_DIR/review_prompt.txt")" \
    --name "$JOB_NAME" --provider "$PROVIDER" --model "$MODEL" \
    --reasoning-effort low --timezone "$TZ_NAME" \
    --monitor-script "$MONITOR" --deliver local --paused
fi

# Constrain only this job to the reviewer tool. Hermes's current cron CLI
# does not expose --enabled-toolsets; protect concurrent gateway writes with
# the same flock lock file cron.jobs uses, then update this one authored field.
python3 - "$HERMES_HOME" "$JOB_NAME" <<'PY'
import fcntl,json,os,stat,sys,tempfile
from pathlib import Path
root=Path(sys.argv[1]); cron=root/'cron'; path=cron/'jobs.json'; name=sys.argv[2]
with open(cron/'.jobs.lock','a+') as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    with open(path,encoding='utf-8-sig') as f: raw=json.load(f)
    records=raw['jobs'] if isinstance(raw,dict) else raw
    matches=[j for j in records if j.get('name')==name]
    if len(matches)!=1: raise SystemExit('Expected exactly one reviewer job; refusing unsafe update.')
    job=matches[0]
    job['enabled_toolsets']=['memory-reviewer','no_mcp']
    fd,tmp=tempfile.mkstemp(prefix='.jobs-memory-reviewer-',dir=cron)
    try:
        os.fchmod(fd,stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(raw,out,indent=2,ensure_ascii=False);out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
PY
hermes cron resume "$JOB_NAME"
printf '\nConfigured %s to review pending memory with %s / %s.\n' "$JOB_NAME" "$PROVIDER" "$MODEL"
echo 'Future plugin code updates: hermes plugins update memory-reviewer'

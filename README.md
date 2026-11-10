# Hermes Memory Reviewer

A native [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets a **Grok-pinned scheduled agent** review pending built-in memory changes individually. Review actions: **list, inspect, revise, approve, reject**.

## Why

Hermes stages memory patches and removals created by unattended background reviews because they may delete useful or safety-critical context. This plugin lets a separate Grok reviewer inspect the exact proposed operation **and the current memory entries** before deciding what to do.

A valid review run can:

- **Approve** a grounded update through Hermes' built-in pending replay and exact-entry pinning.
- **Reject** a duplicated, unrelated, unverified, or harmful proposal.
- **Revise** the existing pending proposal (e.g. convert an unrelated patch into a separate `add`), inspect it again, and then approve or reject.
- **Leave protected changes pending** for a human when a standing permission boundary would be modified.

The plugin stores a per-decision audit log under `$HERMES_HOME/memory-reviewer/audit.jsonl`, plus pre-change snapshots under `$HERMES_HOME/backups/memory-reviewer/`. Neither directory is in this repository.

## Install and update

```sh
hermes plugins install alxspiker/hermes-memory-reviewer --enable
bash "$HOME/.hermes/plugins/memory-reviewer/setup.sh"
```

Once installed from Git, future releases are retrieved with:

```sh
hermes plugins update memory-reviewer
```

The user's provider credentials, memories, approvals, and scheduler state remain local and are **never** in the public repository. Updating code does not require re-running setup, provided the job and monitor wrapper still exist.

### First-time setup

The plugin does not silently schedule model calls when installed. Run `setup.sh` once after installing to configure the native Hermes cron job, monitor wrapper, and built-in memory approval setting. It creates a **Grok-pinned** reviewer that checks for changes once a minute and has **only** the `memory-reviewer` toolset with all MCP tools excluded (`no_mcp`).

Optional environment variables for other deployments:

```sh
HERMES_REVIEW_PROVIDER=xai-oauth \
HERMES_REVIEW_MODEL=grok-4.20-0309-reasoning \
HERMES_REVIEW_TIMEZONE=UTC \
bash "$HOME/.hermes/plugins/memory-reviewer/setup.sh"
```

The job uses Hermes's native `cron` scheduler and your existing provider authentication. A change to the pending queue wakes Grok at the next minute tick. No model runs on unchanged checks. A change back to an empty queue may cause one extra no-op run. The monitor wrapper points to the plugin's tracked `monitor.py`, so **updates to the monitor logic take effect after `hermes plugins update` without copying files again**.

The review job name must remain `pending-memory-grok-reviewer`: the tool checks that cron execution identity before allowing decisions. Hindsight's external memory provider is independent; `memory.write_approval` governs Hermes's built-in `MEMORY.md` and `USER.md` writes.

## Security and constraints

- **No blind auto-approval.** Grok must inspect current entries before choosing one pending record at a time.
- **Pinned destructive edits.** Revisions may not introduce a destructive target that was not pinned in the original proposal.
- **Protected instructions.** Destructive changes to recognized permission-boundary entries are blocked from automatic approval; humans use `/memory approve <id>` if appropriate.
- **Atomic writes.** Approvals replay the proposal with Hermes's native memory store and its character-budget checks.
- **Audit and backups.** Snapshots are created before changing a proposal or memory. Failures leave pending proposals intact.
- **Prompt injection.** Pending text is untrusted and can contain instructions; the scheduled agent is explicitly told to ignore them.
- **Least privilege.** Only the dedicated cron execution identity can call the review tool. Session toolsets are restricted to cron/CLI, and ordinary CLI calls are denied.

The model still makes probabilistic judgments. For maximum safety, review changes to identity or standing authorization manually rather than relying on keyword protection alone.

## Testing

```sh
python3 -m py_compile __init__.py monitor.py
hermes plugins validate "$HOME/.hermes/plugins/memory-reviewer"
```

See Hermes cron history for the actual executed decisions: `hermes cron runs <job-id>`.

## License

MIT.

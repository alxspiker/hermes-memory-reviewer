# Hermes Memory Reviewer

A native [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that uses a **dedicated, model-selectable scheduled agent** to review staged changes to Hermes' built-in memory. The reviewer can **list, inspect, revise, approve, and reject** individual pending proposals. No Grok subscription required: choose any provider and model supported by your configured Hermes installation.

## What it does

Hermes holds some unattended memory patches and removals in a pending queue rather than applying them blindly. The reviewer inspects the proposed operation **and the current memory entries** before deciding.

- **Approve** verified changes using Hermes' built-in replay and exact-entry pinning.
- **Reject** duplicate, unrelated, unverified, or harmful changes.
- **Revise** a salvageable proposal, inspect it again, then approve or reject.
- **Hold for a human** when a destructive change touches a recognized standing permission boundary.

It stores per-decision audit records under $HERMES_HOME/memory-reviewer/audit.jsonl and pre-change snapshots under $HERMES_HOME/backups/memory-reviewer/. These directories are private to the installing machine.

## Install

~~~sh
hermes plugins install alxspiker/hermes-memory-reviewer --enable
bash "$HOME/.hermes/plugins/memory-reviewer/setup.sh"
~~~

The first run offers an **interactive provider and model picker**. It includes your current Hermes default, configured custom provider IDs, and common supported provider names. Models already configured for the selected provider are offered; you can always enter an exact model ID. Suggestions are from **local config, not a live availability/credential check**. The picker makes no model inference calls.

Setup configures the native Hermes cron scheduler, a monitoring wrapper, and memory.write_approval. The reviewer checks the queue once a minute. An unchanged queue **does not call the model**. A changed queue triggers the agent at the next tick (clearing a previously nonempty queue can cause one extra no-op run).

The job is limited to memory-reviewer and no_mcp; it does not inherit general MCP tool access. It uses its own model pin and **does not change your main Hermes model**.

### Configure without a wizard

~~~sh
bash "$HOME/.hermes/plugins/memory-reviewer/setup.sh" \
  --provider openrouter --model "your/provider-model-id" --non-interactive
~~~

You can also use environment variables:

~~~sh
HERMES_REVIEW_PROVIDER=xai-oauth \
HERMES_REVIEW_MODEL=grok-4.20-0309-reasoning \
bash "$HOME/.hermes/plugins/memory-reviewer/setup.sh" --non-interactive
~~~

The optional --timezone America/Edmonton sets the job's timezone. New jobs default to UTC; existing jobs keep their timezone unless explicitly changed.

**The selected provider must already be configured and authenticated in Hermes.** The exact model ID must be supported by that provider. Setup does not create credentials or verify availability by sending an API request.

### Change the model later

Just rerun:

~~~sh
bash "$HOME/.hermes/plugins/memory-reviewer/setup.sh" --configure
~~~

Or provide a new provider and model with the noninteractive flags. An existing job is **edited in place**: its ID, history, schedule and paused/active state are preserved. Its legacy pending-memory-grok-reviewer name is migrated to pending-memory-reviewer without creating a second job. Setup refuses ambiguous duplicates rather than guessing.

### Update the plugin

~~~sh
hermes plugins update memory-reviewer
~~~

The monitoring wrapper loads monitor.py from the installed plugin directory, so Git updates change monitoring logic without copying files. Legacy Grok-named jobs continue to be authorized until you rerun setup. Credentials, approvals, memories and scheduler state remain local.

## Safety boundaries

- **No blind auto-approval.** The prompt requires inspecting current entries before deciding per proposal.
- **Pinned destructive edits.** Revisions may not introduce destructive targets not pinned in the original.
- **Protected instructions.** Recognized permission-boundary edits remain pending for human approval (/memory approve <id>).
- **Atomic store writes.** Approvals use the native Hermes memory store and character-budget limits.
- **Audit and backups.** Snapshots are created before changes; failures retain pending proposals.
- **Prompt injection defense.** Pending content is untrusted data, not instructions.
- **Least privilege.** Only the named cron execution can use the review tool.

Memory review still involves probabilistic model judgments. Consider reviewing identity, permissions and standing authorizations manually. Hindsight's external memory provider is separate: memory.write_approval applies to Hermes' built-in MEMORY.md and USER.md, **not** Hindsight's external memory store.

## Testing

~~~sh
python3 -m py_compile __init__.py monitor.py configure.py
python3 -m unittest discover -s tests -v
hermes plugins validate "$HOME/.hermes/plugins/memory-reviewer"
~~~

Inspect actual run history with hermes cron runs <job-id>.

## License

MIT.

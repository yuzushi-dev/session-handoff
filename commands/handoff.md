---
description: Create a clean handoff or migrate the active session between Claude Code and Codex.
argument-hint: [migrate claude|migrate codex|focus]
---

Follow the `session-handoff` skill exactly.

If `$ARGUMENTS` starts with `migrate`, use migrate mode. The second argument must be the target client (`claude` or `codex`). Resolve the exact active native session ID as required by the skill, then call `handoff_migrate`. When the result says `auto_switch_requested: true`, stop working in this session: the supervisor will terminate the source client before conversion and resume the target session after migration. If it says false, report the reason and do not run `session-migrate` against the active transcript yourself.

Otherwise create a complete handoff for the current session, focused on `$ARGUMENTS`. Call `handoff_create` with `auto_switch: true`. When the result says `auto_switch_requested: true`, stop working in this session: the supervisor will terminate this client and launch a fresh one with a pre-filled, unsent reference to the handoff. If it says false, report the manual resume command and the reason automatic switching was unavailable.

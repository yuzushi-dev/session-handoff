---
description: Create a clean handoff or migrate the active session between Claude Code and Codex.
argument-hint: [migrate claude|migrate codex|focus]
---

Follow the `session-handoff` skill exactly.

If `$ARGUMENTS` starts with `migrate`, use migrate mode. The second argument must be the target client (`claude` or `codex`). Resolve the exact active native session ID as required by the skill, then call `handoff_migrate`. If the result says `auto_switch_requested: true`, stop working in this session. The supervisor will terminate the source client before conversion and resume the target session after migration. If it says false, report the reason and do not convert an active transcript that the client may still be appending to.

Otherwise create a complete handoff for the current session, focused on `$ARGUMENTS`. Call `handoff_create` with `name: "next.md"` and `auto_switch: true`. Central references are immutable (`handoff://project-uuid/handoff-uuid`); resume them through `handoff_read`. If the result says `auto_switch_requested: true`, stop working in this session. If it says false, report the manual resume command and why automatic switching was unavailable. Use `path` explicitly for legacy workspace files.

---
description: Create a handoff and open a fresh supervised session with a draft reference.
argument-hint: [focus]
---

Create a complete handoff for the current session, focused on: $ARGUMENTS.

Follow the `session-handoff` skill exactly. Call `handoff_create` with
`auto_switch: true`. When the result says `auto_switch_requested: true`, stop
working in this session: the supervisor will terminate this client and launch a
fresh one with a pre-filled, unsent reference to the handoff. If it says false,
report the manual resume command and the reason automatic switching was unavailable.

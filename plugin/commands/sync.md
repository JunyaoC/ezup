---
description: Backfill — pick PAST sessions to share. Usage — /ezup:sync [all|7d|30d|a date]
---

The user passed: `$ARGUMENTS`

Run `ezup sync $ARGUMENTS` with the Bash tool (bare `ezup sync` if no
argument). It opens an interactive picker, so run it in the foreground and let
the user drive — do not pipe input into it, do not pick sessions for them.

Whole past transcripts are uploaded, chosen one by one; nothing is pre-ticked,
and that is deliberate. Never suggest "select all". Relay the result
faithfully. Do not run any other command.

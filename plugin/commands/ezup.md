---
description: Control ezup session sharing. Usage — /ezup on | off | status | sync [all|7d|30d|a date]
---

## Your task

The user wants to control ezup sharing. The argument they passed is:
`$ARGUMENTS`

Run exactly one of the following with the Bash tool, based on the first word:

- `on` → run `ezup share on`
- `off` → run `ezup share off`
- `status`, or no argument → run `ezup share status`
- `sync` → run `ezup sync <window>` where `<window>` is the second word
  (`all`, `7d`, `30d`, a date…; omit it if the user gave none). This opens an
  interactive picker for backfilling PAST sessions, so run it in the
  foreground and let the user drive it — do not pipe input into it, do not
  pick sessions for them.

Then relay the command's output to the user faithfully. Points that matter:

- If the command reports sharing turned ON, make sure the user sees where the
  bytes go (the store URL from `ezup share status`) and that only work from
  this point onward is shared — nothing recorded before the opt-in is sent.
  If they want history shared too, that is `/ezup sync`.
- `sync` uploads whole past transcripts, chosen one by one — nothing is
  pre-ticked, and that is deliberate. Never suggest "select all".
- If it refuses (for example the repo's `.ez/config.json` says `never`, or the
  policy needs acknowledging with `ezup share ack`), show the refusal reason
  verbatim rather than paraphrasing it.
- If `ezup` is not installed, say so and point at
  https://github.com/JunyaoC/ezup — the plugin alone shares nothing.

Do not run any other command. Do not decide for the user.

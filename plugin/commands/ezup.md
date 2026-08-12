---
description: Control ezup session sharing. Usage — /ezup on | off | status | sync [all|7d|30d] | token <who>
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
- `token show` → run `ezup token show`. It prints this machine's device token
  — the login for the web viewer at the store URL. If its output warns that
  this session is being shared, surface that warning prominently.
- `token` (anything else) → run `ezup token mint --name "<rest of the
  arguments>"`. If no name was given, ask who the token is for first — do not
  invent one.

Then relay the command's output to the user faithfully. Points that matter:

- If the command reports sharing turned ON, make sure the user sees where the
  bytes go (the store URL in the output) and that only work from this point
  onward is shared — nothing recorded before the opt-in is sent. If they want
  history shared too, that is `sync`.
- `sync` uploads whole past transcripts, chosen one by one — nothing is
  pre-ticked, and that is deliberate. Never suggest "select all".
- A minted token is shown ONCE and never again: tell the user to copy it now.
  It grants read-only access to this device's sessions and is revoked with
  `ezup token revoke` — bare when only one token is active, or by name. Never print the token yourself a second time.
- If it refuses (for example the repo's `.ez/config.json` says `never`, or the
  policy needs acknowledging with `ezup share ack`), show the refusal reason
  verbatim rather than paraphrasing it.
- If `ezup` is not installed, say so and point at
  https://github.com/JunyaoC/ezup — the plugin alone shares nothing.

Do not run any other command. Do not decide for the user.

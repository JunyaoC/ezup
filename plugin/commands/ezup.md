---
description: Turn ezup session sharing on or off, or check what is being shared. Usage — /ezup on | off | status
---

## Your task

The user wants to control ezup sharing for THIS session. The argument they
passed is: `$ARGUMENTS`

Run exactly one of the following with the Bash tool, based on the argument:

- `on` → run `ezup share on`
- `off` → run `ezup share off`
- `status`, or no argument → run `ezup share status`

Then relay the command's output to the user faithfully. Points that matter:

- If the command reports sharing turned ON, make sure the user sees where the
  bytes go (the store URL from `ezup share status`) and that only work from
  this point onward is shared — nothing recorded before the opt-in is sent.
- If it refuses (for example the repo's `.ez/config.json` says `never`, or the
  policy needs acknowledging with `ezup share ack`), show the refusal reason
  verbatim rather than paraphrasing it.
- If `ezup` is not installed, say so and point at
  https://github.com/JunyaoC/ez-change-log — the plugin alone shares nothing.

Do not run any other command. Do not decide for the user.

---
description: Share THIS session's transcript with the team store, from this point forward
---

Run `ezup share on` with the Bash tool and relay its output faithfully.

- Make sure the user sees where the bytes go (the store URL in the output) and
  that only work from this point onward is shared — nothing recorded before
  the opt-in is sent. If they want history too, that is `/ezup:sync`.
- If it refuses (a repo `.ez/config.json` saying `never`, or a policy needing
  `ezup share ack`), show the refusal reason verbatim.
- If `ezup` is not on PATH, say so and point at
  https://github.com/JunyaoC/ezup — the plugin alone shares nothing.

Do not run any other command. Do not decide for the user.

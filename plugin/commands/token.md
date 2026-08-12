---
description: Mint a read-only token so an operator/PM can pull your shared sessions. Usage — /ezup:token <who it is for>
---

The user passed: `$ARGUMENTS`

- If `$ARGUMENTS` names who the token is for, run
  `ezup token mint --name "$ARGUMENTS"` with the Bash tool.
- If it is empty, ask who the token is for first — the name is how they will
  recognise it in `ezup token list` when revoking later. Do not invent one.

Relay the output faithfully. The token value is shown ONCE and never again:
tell the user to copy it now and hand it to the operator, who uses it as
`EZUPDATE_TOKEN` with the same store URL. It grants read-only access to this
device's sessions — no writing, no deleting, no minting — and can be revoked
any time with `ezup token revoke <id>`.

Do not run any other command. Never print the token yourself a second time.

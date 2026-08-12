# PM quickstart — a full member: share your own work AND read the team's

For a manager/operator on a fresh machine that already has **uv** and the
**claude** CLI (signed in). A PM is a full member: they enrol as a **device**
(to share their own sessions and mint reader keys) and hold a **keyring** (to
read teammates' sessions). Both live in one config.

A PM who only wants to *read* can skip the device (section 2b/3-share) and use
just the store URL + keyring.

## 1. Install (once)

```bash
git clone https://github.com/JunyaoC/ezup && cd ezup
./setup.sh
```

`setup.sh` creates the venv with uv, installs the package, and links `ezup` onto
your PATH. It also installs the one dependency (`cryptography`, for the E2E
encryption).

## 2. Point at the team store, and enrol as a device (once)

Set the store, then enrol. Enrolling needs the **admin token** (the store owner
holds it — if the PM set up the Cloudflare store, that is them):

```bash
export EZUPDATE_STORE=https://ezupdate.nyf.workers.dev

# store owner enrolling themselves (admin token in ~/.ezchangelog/admin-token
# or $EZUP_ADMIN_TOKEN):
ezup device enroll --name pm

# OR the store owner enrols the PM remotely and hands them the printed key:
#   ezup device mint --name pm         # prints token + device_id
# the PM then writes those into ~/.ezchangelog/config.json as "token" and
# "device_id" (with "store").
```

`device enroll` generates the device key ON THIS MACHINE (the server only ever
gets its hash) and writes `~/.ezchangelog/config.json` for you. It refuses to
overwrite an existing device — use `--force` only if you mean to replace it
(that orphans the old device's sessions).

**Read-only PM instead?** Skip enrolment; just write the store URL:

```bash
mkdir -p ~/.ezchangelog
echo '{ "store": "https://ezupdate.nyf.workers.dev", "author": "pm" }' \
  > ~/.ezchangelog/config.json && chmod 600 ~/.ezchangelog/config.json
```

## 2b. Share your own sessions (optional, if enrolled as a device)

Same as any developer:

```bash
ezup hook install        # wires the Stop/SessionEnd hooks + statusline
# then inside any Claude session:  /ezup on
```

## 3. Add each teammate's reader key (once per person)

Each developer runs `ezup token mint --name pm` on their own machine and sends
you the `ezr_...` key it prints once. Add each:

```bash
ezup keyring add ezr_ALICEKEY --label alice
ezup keyring add ezr_BOBKEY   --label bob
ezup keyring list
```

The key is stored keyring-private (0600) and never printed back. A key only
grants read access to the sessions its owner chose to share — nothing else.

## 4. Run today's pipeline

Two commands. `pull` fetches from the remote **per keyring key** and decrypts
locally; `collect --include-pulled` runs the full journal pipeline over what was
pulled:

```bash
ezup pull                                # decrypt the team's shared sessions
ezup collect 1d --include-pulled -i      # today; pick sessions, build the journal
```

Windows are the same as always: `1d` today, `7d` a week, `30d` a month, `all`
everything. Drop `-i` and add `--yes` to take every matched session without the
picker (what a cron would use):

```bash
ezup pull && ezup collect 7d --include-pulled --yes
```

The journal (`journal.html` + `journal.md`) lands in
`~/.ezchangelog/journals/<timestamp>/`, grouped by person as well as project and
date. The path is printed at the end.

## How the remote input works

`ezup pull` is the only thing that talks to the store. For each key in the
keyring it lists that key's sessions, fetches the ciphertext chunks, verifies
each GCM tag, and writes the decrypted transcript to
`~/.ezchangelog/pulled/<author>/<session>.jsonl`. So **what you can journal is
exactly what your keyring can decrypt** — add a key, that person appears; remove
it, they are gone. The store only ever hands out ciphertext; the decryption
happens on your machine because you hold the keys.

`collect --include-pulled` then treats those pulled transcripts as inputs
alongside any local ones, and the pipeline (BRIEF/COMPOSE via `claude -p`, which
is why you need the claude CLI signed in) turns them into the journal.

## Notes

- **A cron/daily run** is just the two `--yes` commands above on a timer. For a
  headless box without an interactive `claude` login, use the container runner
  (`runner/`) with an LLM API key instead.
- **Revoking a key** (`ezup token revoke`, on the *dev's* side) stops new grants;
  transcripts already pulled stay on the PM's disk, and data keys already held
  stay usable until each session's generation rotates. Delete
  `~/.ezchangelog/pulled/<author>/` to remove what was fetched.
- **`--include-pulled` also scans this machine's own `~/.claude/projects`.** On a
  pure PM machine that is empty, so you get only the team. If the PM also codes
  and wants only the team's sessions, that would need a `--pulled-only` flag
  (not built yet).

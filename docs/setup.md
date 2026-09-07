# Setting up your own Assistant Memory — a runbook for an LLM agent

> **Audience: an LLM coding agent (Claude, Codex, …) setting this up for its operator.**
> You (the agent) execute everything you can — files, Docker, downloads, verifying. A few steps
> only a human can do (install Docker, create a Google OAuth app, enter secrets, click a browser
> consent). For those, **read the operator the `SAY TO OPERATOR` script — rendered in the
> operator's language (step 0) — and wait** for them to finish before continuing. This file is
> written in English so any agent can follow it; what you SAY to the human follows their language.

Assistant Memory is a self-hosted, access-controlled knowledge-graph service that your LLM
clients connect to over MCP. This runbook produces an instance authenticated with **OAuth only** —
your clients mint their own tokens through the browser consent flow; you never issue or paste a
raw token. End state: the install status reports **installed**, the operator's LLM client sees the
memory tools, and you have walked the operator through the first-steps guide.

**The service tells you where you are.** The instance computes its own install state from facts —
five stages: `schema`, `config`, `seed`, `owner`, `validated` — and while the first four are not
all closed it refuses substantive requests with a structured answer naming the **first unclosed
stage** and what closes it (HTTP `503 {"error": "not_installed", "stage": ..., "closes_with":
...}`; the same three fields as a tool error over MCP). `GET <AM_BASE_URL>/install/status` shows
the full stage list with each predicate's current answer at any moment — when in doubt, read it.

**Verify after every step; red means STOP.** Every step below ends with a check you can run. On a
failed check you stop and fix — you never improvise a workaround; a run that needed one is a
failed run. Failures of the two earliest stages can precede any serving process (migrations and
configuration parsing run before the server starts) — those are **pre-serve states**, diagnosed
from the container's exit status and logs (`docker compose logs app`), not from an endpoint.

**Your tree is a git consumer.** You never edit or commit tracked repository files on an
installed instance. Everything alive on the instance — `.env`, the observations file, runtime
data — is git-excluded by the shipped `.gitignore`, so `git status` stays clean; that cleanliness
is what makes updates (§12) a plain `git fetch`/`checkout` with no conflicts.

Legend: **[AGENT]** you do it · **[SAY TO OPERATOR]** render in the operator's language, say,
then wait · **[VERIFY]** check before moving on.

---

## 0. Step zero — three human decisions, before any technique

Ask the operator, in plain language, and record the answers:

1. **Which language do you want to be spoken to in?** From this answer on, everything you SAY to
   the human is in that language (this runbook and every machine text stay English). **[AGENT]**
   Persist the answer immediately — write it into the instance's env configuration file as
   `AM_OPERATOR_LANGUAGE=<their answer>` (create `.env` from `.env.example` first if needed; this
   key is not a secret and is yours to write; the server never reads it). Re-read it on any rerun
   or fresh session; in §11 you will seed it into the operator profile as their first preference.
2. **Where does the service run?** — see the three variants in §1; ask as a plain choice
   ("on this machine, on a remote machine you own, or in a cloud").
3. **Which Google account is the owner?** The instance authenticates by Google OAuth, and one
   account — this one — becomes the owner.

**[AGENT] Start the observations file.** During the install you learn things about the operator
from plain conversation — language and register, the questions they ask, preferences stated in
passing. Keep them in `OPERATOR_OBSERVATIONS.md` in the repository root: a human-readable file,
git-excluded by the shipped `.gitignore`, carrying **no secrets** ever. It seeds the operator
profile later (§11) as proposals the operator accepts — it is a hint file, never an authority,
and the first-steps guide names it to the operator (transparency, not a dossier).

---

## 1. The three deployment variants

What the product REQUIRES is a property, not a tool: **an address the operator's clients can
reach**, and for any non-local variant **a stable public https URL** — that URL is the OAuth
issuer, and changing it re-breaks every connector and the Google redirect. Concrete tools below
are examples; the developer uses ngrok, you are free to choose.

- **(A) Local machine** — server and clients on the same machine. `AM_BASE_URL =
  http://localhost:8000`. Google permits `http://localhost` redirect URIs, so no domain or TLS.
  *Proof status: proven by the author's acceptance runs on the working repository; NOT re-walked from this snapshot (README, "What was and was not checked for this release").*

- **(B) Remote machine** — a box you own, reachable over the internet at a stable public https
  URL. `AM_BASE_URL = https://<your-domain>`. Two example routes to the URL:
  - **ngrok reserved domain** (scripted here): an ngrok account + a reserved domain; the compose
    **`remote` profile** ships an `ngrok` service, so `docker compose --profile remote up -d`
    brings the tunnel up with the stack. The tunnel keys on the app's LIVENESS, so it rises
    immediately on a fresh install and exposes exactly the install surface until the install
    completes — the owner login it serves is how the install finishes.
  - **your own domain + reverse proxy + TLS** — full control, more setup; you provide the proxy.

  *Proof status: proven by the author's acceptance runs on the working repository, over the ngrok route; NOT re-walked from this snapshot (README, "What was and was not checked for this release").*

- **(C) Cloud** — a rented instance with its own public address. A cloud machine with a public
  address **needs no tunnel** — do not drag the remote recipe along; give the app a stable https
  URL by the cloud's own means (a load balancer, a proxy, a public DNS name with TLS).
  *Proof status: **expected but untested** — documented from the same properties, walked by no
  acceptance run yet. You would be the first; the stop-on-red rule matters double.*

For (B)/(C): everything is OAuth-gated, and until the install completes the service refuses all
substantive requests by construction — but still **do not expose the instance before the config
stage is closed** (OAuth configured): the runbook order below already ensures that.

> For a remote/cloud server, run every command below **on that server** (e.g. over SSH). Only
> the browser steps (OAuth consent, owner login) happen on the operator's own device, against
> the public URL.

**Record now:** variant **A/B/C** and the resulting `AM_BASE_URL`.

---

## 2. Prerequisites & detect the environment

The operator needs: a host with admin rights, **Docker**, **git**, and a **Google account** (§4
covers not having one). **[AGENT]** detect what's present on the host the server will run on:

```bash
uname -s 2>/dev/null || ver          # Linux / Darwin / "Microsoft Windows ..."
docker --version ; docker compose version ; git --version
```

If `docker`/`git` are missing — **[SAY TO OPERATOR]**, per OS:

- **Windows / macOS** — "Install **Docker Desktop** from
  https://www.docker.com/products/docker-desktop/, launch it, wait until it says *Engine
  running*. Install **git** from https://git-scm.com/downloads (macOS: `xcode-select
  --install`). Tell me when Docker shows running."
- **Linux** — "Install Docker Engine + the compose plugin per
  https://docs.docker.com/engine/install/, then `sudo usermod -aG docker $USER` and re-login.
  Install git via your package manager. Tell me when `docker ps` works."

Also check the app port is free (default **8000**):
```bash
# Linux/macOS:  lsof -i :8000        Windows:  netstat -ano | findstr :8000
```
If 8000 is taken, choose a free port **now** and use it **everywhere below** — it changes
`AM_BASE_URL`, the Google **redirect URI** (§4), and the compose host-port mapping.

**[VERIFY]** `docker ps` and `docker compose version` both succeed.

---

## 3. Get the code

**[AGENT]** (on the server, for B/C):
```bash
git clone <the repository URL your operator shared>
cd assistant_memory
```

**[VERIFY]** `git status` reports a clean tree. It stays clean for the life of the instance —
nothing in this runbook edits a tracked file.

---

## 4. Create a Google OAuth app — the one real human step (~10 min, one-time)

Authentication is **Google OAuth only**. The operator creates an OAuth client so they can log in
as the instance **owner**. This section assumes the operator has never seen Google Cloud Console
and may not have a Google account. It is built in three layers.

**Layer 1 — the goal state (this does not rot).** When this step is done, all of this exists:

- a Google account the operator controls (the owner account from step 0);
- a Google Cloud project with an **OAuth client** of type *Web application*;
- that client's **Authorized redirect URI** equals EXACTLY `<AM_BASE_URL>/auth/callback`;
- the **consent screen** is in *Testing* mode with the operator's own email among **Test
  users** (no verification needed; up to 100 test users);
- the operator holds the **Client ID** and **Client secret**.

**Layer 2 — the click path** *(verified against the real interface as of this release)*.
**[SAY TO OPERATOR — render in their language]**

> "Create a Google OAuth app (one-time):
> 1. If you don't have a Google account: create one at https://accounts.google.com/signup.
> 2. Open https://console.cloud.google.com/ and create (or pick) a project.
> 3. **APIs & Services → OAuth consent screen**: choose **External**, fill the app name and your
>    email. Under **Test users**, add your own Google email. Leave it in *Testing* mode.
> 4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
>    - Application type: **Web application**
>    - **Authorized redirect URI** — paste EXACTLY: `<AM_BASE_URL>/auth/callback`
>    - Create. Keep the **Client ID** and **Client secret** open — you will enter them into the
>      configuration file yourself in the next step; do not send them to me.
> Tell me the **Google email** you'll log in with — it becomes the owner."

**Layer 3 — the divergence rule (for you, the agent).** If the Console has drifted from the
click path above, do **not** improvise from memory: consult Google's current official
documentation and lead the human by it, holding Layer 1 as the criterion of done. The objective
verification is unchanged either way: the issued credentials work — the owner's login (§8)
succeeds.

**[WAIT]** for: the owner email (only that — the secrets never pass through you).

---

## 5. Configure `.env` — the human enters the secrets, you verify presence

**[AGENT]** Prepare the non-secret half:

```bash
cp -n .env.example .env   # keep it if step 0 already created it
```

**[PowerShell]** equivalent:
```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Then set (you may write these — they are not secrets):

| Key | Value |
|---|---|
| `AM_DATABASE_URL` | leave the compose default (compose overrides it to the in-network DB) |
| `AM_BASE_URL` | `http://localhost:8000` (A) or `https://<your-domain>` (B/C) — must match the OAuth redirect host |
| `AM_OWNER_EMAIL` | the operator's Google email (pins who the owner is) |
| `AM_OPERATOR_LANGUAGE` | already written at step 0 |

Leave `AM_CONVENTIONS_SPACE_ID` and `AM_FEEDBACK_SPACE_ID` **unset** — on a stock install the
structural bootstrap creates the conventions home itself and records its id in the database;
the feedback capability is not part of the stock install at all.

For variant **B via the optional ngrok example** (the `remote` compose profile), two keys that
are NOT in `.env.example` are added to `.env`: `AM_TUNNEL_DOMAIN` (the reserved domain,
matching `AM_BASE_URL`'s host) and `NGROK_AUTHTOKEN` — both come from the operator's ngrok
dashboard; the authtoken is a secret and follows the secret channel below. Any other way of
making `AM_BASE_URL` reachable needs neither key.

**The secrets — `AM_GOOGLE_CLIENT_SECRET`, `AM_GOOGLE_CLIENT_ID`, `AM_SESSION_SECRET` (and
`NGROK_AUTHTOKEN` if used) — are entered by the human, in a session you are not part of.** You
identify the file and the keys, then wait. **[SAY TO OPERATOR — render in their language]**

- Variant A (local): "Open `<absolute path>/.env` in your own editor. Fill
  `AM_GOOGLE_CLIENT_ID` and `AM_GOOGLE_CLIENT_SECRET` with the values from the Google Console.
  For `AM_SESSION_SECRET`, run `python -c "import secrets; print(secrets.token_urlsafe(48))"`
  in your own terminal and paste the output. Save, and tell me when done."
- Variant B/C (remote): "Run this one command **in your own terminal** (not mine):
  `ssh <user>@<server> nano <absolute path>/.env` — then fill the same keys as above, save,
  and tell me when done."

**[VERIFY — redacted presence only.]** You never read the values back. Check that the keys are
set and carry no placeholder:

```bash
grep -E "^(AM_GOOGLE_CLIENT_ID|AM_GOOGLE_CLIENT_SECRET|AM_SESSION_SECRET|AM_OWNER_EMAIL)=" .env | sed -E 's/=.+/=<set>/'
```

**[PowerShell]** equivalent (the placeholder count must be `0`):
```powershell
Select-String -Path .env -Pattern '^(AM_GOOGLE_CLIENT_ID|AM_GOOGLE_CLIENT_SECRET|AM_SESSION_SECRET|AM_OWNER_EMAIL)=' | ForEach-Object { ($_.Line -split '=',2)[0] + '=<set>' }
(Select-String -Path .env -Pattern 'change-me' -AllMatches).Matches.Count
```

Every listed key must print `=<set>`, and `AM_SESSION_SECRET` must not still be the shipped
`change-me…` placeholder (`grep -c "change-me" .env` → `0`). The service enforces the same facts
itself: the `config` stage stays open — and substantive requests stay refused — while any
required setting is missing or a placeholder.

---

## 6. Download the embedding model

The instance uses **bge-m3** (multilingual, ~2 GB) for semantic search. Download it **into the
cache volume first**, as an explicit step with visible progress — so the stack comes up warm.

**[AGENT]**
```bash
docker compose run --rm app python -c "from assistant_memory.search import get_embedder; get_embedder().embed(['warmup'])"
```
The cache is a **named Docker volume**: it persists across restarts, downloaded once.

> Lighter option: for a quick trial set `AM_EMBEDDING_PROVIDER=deterministic` in `.env` and skip
> this step — search still works via full-text; semantic quality is lower.

---

## 7. Build with the release identity, bring up the stack

**The build must know what code it is.** The commit id is baked into the image and the server
compares it against the recorded release after every restart — an image built without it cannot
reach **installed** (the status names the refusal). This is a **required** input, derived from
the checkout:

**[AGENT]** — build (bash; PowerShell: set `$env:GIT_COMMIT`/`$env:GIT_TAG` the same way
first). The tag is the optional second half of the identity — non-empty exactly when the
checkout sits on a release tag, so a tag-deployed install records its tag as the base for
later updates:
```bash
GIT_COMMIT=$(git rev-parse HEAD) GIT_TAG=$(git describe --tags --exact-match 2>/dev/null || true) docker compose build app
```

**[PowerShell]** equivalent:
```powershell
$env:GIT_COMMIT = git rev-parse HEAD
$env:GIT_TAG = ""; try { $env:GIT_TAG = git describe --tags --exact-match 2>$null } catch {}
docker compose build app
```

Bring up — variant **A** / **C**:
```bash
docker compose up -d
```
Variant **B with ngrok** (also starts the tunnel):
```bash
docker compose --profile remote up -d
```

On startup the app runs migrations, computes its install state, records the release identity of
the code it brought up, and **bootstraps itself** (idempotent): the owner record, a default
`personal` space, the **conventions home space** (created and recorded in one transaction), and
the conventions projection seeded into it.

**[VERIFY]** (against `AM_BASE_URL`):
```bash
curl -s <AM_BASE_URL>/health           # {"status":"ok"}   liveness — the install surface serves
curl -s <AM_BASE_URL>/install/status   # the five stages with answers
```
Expect in `/install/status`: `schema`, `config`, `seed` all `ok: true`; `first_unclosed:
"owner"`; `releases.executing` a 40-hex commit and `releases.refusal: null`. If a stage that
should be closed is not, its `closes_with` and `detail` say what to do; a container that exits
before serving is a pre-serve state — read `docker compose logs app`. Do **not** proceed until
only `owner` and `validated` remain open.

---

## 8. Owner login (closes the `owner` stage)

**[SAY TO OPERATOR — render in their language]**
> "Open **`<AM_BASE_URL>/login`** in your browser and click **Log in with Google**. Use the
> same Google account whose email you gave me. After it returns, you're the owner. (If you see
> *not admitted*, you logged in with a different account than the owner email.)"

(The `/login` path matters: until installation completes, the site root — like every
non-install route — answers with the `not_installed` refusal, by design. `/login` is part of
the install surface and serves the login page before the gate opens.)

**[WAIT]**, then **[VERIFY]**: `/install/status` now shows `owner` ok and `status:
"validating"` — every install-time fact exists; what remains is the proof that the whole loop
closes, and that proof is the next two steps.

---

## 9. Connect the LLM client over OAuth

The client points at `<AM_BASE_URL>/mcp`. The first tool use runs the browser consent flow —
the operator logs in as owner and approves; no manual token.

- **Claude Code CLI**:
  ```bash
  claude mcp add --transport http assistant-memory <AM_BASE_URL>/mcp -s user
  ```
- **Other CLIs (Codex, …):** add `<AM_BASE_URL>/mcp` as a remote/HTTP MCP server with OAuth.
- **Cloud clients (claude.ai / ChatGPT):** only reachable-from-internet variants (B/C). Add a
  connector pointing at `https://<your-domain>/mcp`; it runs the same OAuth.

**[SAY TO OPERATOR — render in their language]**
> "A browser will open to authorize the memory connector. Log in with your owner Google account
> and click **Allow**. Tell me when it's done."

**[AGENT] For a *stateless* client** — one with no persistent local file store and no
session-start hook of its own (ChatGPT is one; Claude Code is not): the conventions'
recall-before-acting rules cannot self-enforce for it. The ready-made instructions live in
[`docs/conventions/stateless_client_prompt.md`](conventions/stateless_client_prompt.md) and come
in **two layers — the operator must paste BOTH** (empirically, the detailed prompt alone did not
make ChatGPT reach for the tools): Layer 1, a short tool-first rule, into the client's
**global** custom instructions; Layer 2, the detailed prompt, into the specific **Project**
where the connector is attached.

---

## 10. The finishing probe — YOU perform it (closes the `validated` stage)

The install is proven closed by one specific operation, and the server accepts no substitute:
**an authenticated `conventions` tool call by the owner's credential over the real MCP
transport**. The server observes that call itself and writes the completion record — there is no
API to assert "installed", and no other tool closes the stage.

**[AGENT]** From the connected client, **call the `conventions` tool** (no arguments). Expect
`found: true`, a `version`, and the sections. Then:

**[VERIFY]**
```bash
curl -s <AM_BASE_URL>/install/status   # status: "installed"
```
`status` must read `"installed"` — the loop observably closed under this configuration, this
generation, and this release. A stall in `"validating"` after a successful-looking call is
**red**: the call did not run under the owner credential over the real transport (see
Troubleshooting). Also confirm `search` and `list_spaces` return without an auth error.

**[VERIFY — stateless client only, behavioral acceptance test]** On the client where you pasted
the stateless-client prompt, in a **fresh** conversation, ask a project-specific question
**without** any "check memory" phrasing. **Pass** only if the client calls `search`/`traverse`
on its own before answering. If not, check both prompt layers are pasted (§9).

---

## 11. Seed the operator profile: language first, then the observations

**[AGENT]** The step-zero language now becomes the first profile preference — through the
standing preference tool, with the scope stated explicitly (the tool defaults an omitted scope
to the project scope, and this value must be the global member every project inherits):

- call `remember_preference` with `domain: "language"`, `scope: "global"`, and the value from
  `AM_OPERATOR_LANGUAGE` (e.g. "Speak English to me", phrased in the operator's own words
  and language — the value itself is the operator's, this source stays English), as the
  operator's stated preference;
- **[VERIFY]** read `get_operator_profile` and confirm the `language` domain appears in the
  compiled profile.

Then walk the observations file (`OPERATOR_OBSERVATIONS.md`) and propose what belongs in the
profile — as **proposals, not facts**: each `remember_preference` call carries the `inferred`
marker and an **explicit scope** (about the operator as a person → `global`; tied to one
project's material → that project; ambiguous → your judged scope, NAMED in the entry text so the
operator's confirmation is scope-aware). Nothing binds without the operator's own confirmation;
a wrong-scope candidate is corrected by rejection (`confirm_preference` with `disposition:
"reject"`) and a fresh proposal at the corrected scope. The file stays what it is — a local
hint-cache; the graph is the canon.

---

## 12. First steps — the last mandatory act of the install

**[AGENT]** Walk the operator through [`docs/first_steps.md`](first_steps.md) **in their
language**. This is a step of the install, not optional reading: it covers adding an existing
project, declaring a development cycle, preferences and reporting problems, care of the
database, updates, the observations file, and the review settings the first review gate will
ask about. Do not skip it and do not summarize it into one sentence — the operator decides when
they are done with it.

---

## 13. Updates

The repository is alive; updates arrive as **release tags**. A release note (one per release, at
`docs/releases/`) is **descriptive** — everything an update *requires* is enforced by the target
version's install state machine itself, so skipping releases is safe by construction. The update
operation:

```bash
git fetch --tags origin
# read the notes between your installed release and the target, for context:
#   docs/releases/<each release between current and target>.md
git checkout <target-tag>
# RECORD the pending target (the server promotes it only after observing the new code run):
docker compose run --rm app python -m assistant_memory.install.apply --tag <target-tag> --commit $(git rev-parse HEAD)
# rebuild with the identity injected (both halves), restart:
GIT_COMMIT=$(git rev-parse HEAD) GIT_TAG=$(git describe --tags --exact-match 2>/dev/null || true) docker compose build app
docker compose up -d
```

**[PowerShell]** equivalent:
```powershell
git fetch --tags origin
git checkout <target-tag>
$commit = git rev-parse HEAD
docker compose run --rm app python -m assistant_memory.install.apply --tag <target-tag> --commit $commit
$env:GIT_COMMIT = $commit
$env:GIT_TAG = ""; try { $env:GIT_TAG = git describe --tags --exact-match 2>$null } catch {}
docker compose build app
docker compose up -d
```

After the restart the server compares its executing identity against the recorded target and
promotes it to the active installed release on match; the status is **validating** until you
finish exactly as the install finished — **one authenticated `conventions` call** (§10), which
writes the new completion record. **[VERIFY]** `/install/status`: `status: "installed"`,
`releases.active` = the target, `releases.pending: null`. A newly required stage (a migration,
a new setting) surfaces as that stage's named refusal — walk the operator through exactly that
delta, then re-verify.

---

## 14. Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Any endpoint answers `503 {"error":"not_installed", "stage":..., "closes_with":...}` | Working as designed: that stage is unclosed. `GET /install/status` for the full picture; the `closes_with` text is the instruction. |
| Container exits / restarts before serving | A **pre-serve state** (migrations or config parsing failed): `docker compose logs app` — the exit trace names it. Common: `AM_SESSION_SECRET` still the placeholder on a real deployment (the app refuses to start and says so). |
| Browser: `redirect_uri_mismatch` | The Google redirect URI must EXACTLY equal `AM_BASE_URL` + `/auth/callback`. Fix in Google Console or `AM_BASE_URL`, then `docker compose up -d`. |
| Login: **not admitted** (403) | Logged in with a Google account ≠ `AM_OWNER_EMAIL`. Use the pinned account, or the human fixes `AM_OWNER_EMAIL` (their file) and you restart. |
| `seed` stage red after start | Read `docker compose logs app` for the bootstrap line. If it names candidate same-purpose spaces: the recorded home id is missing while conventions projections exist — the operator picks the space in the admin UI (`/spaces` → *set as conventions home*), then restart. |
| `/install/status` shows `releases.refusal: "missing executing identity..."` | The image was built without `GIT_COMMIT` — rebuild per §7. |
| `releases.refusal: "tree updated, old code executing"` | An update recorded a pending target but the restart still runs old code — the rebuild didn't happen or didn't take. Re-run the §13 build+restart; the active release and status stay honest meanwhile. |
| Stuck in `validating` after the probe | The `conventions` call must be **authenticated as the owner over `/mcp`**. A call under another account, or through a non-MCP path, closes nothing. Re-run §10 from the operator's connected client. |
| `/health` slow / stack seems stuck | Did you run §6? Without the pre-download the app fetches ~2 GB on first embed. Watch `docker compose logs -f app`. |
| Port 8000 already in use | Change the host port in `docker-compose.yml` and set `AM_BASE_URL` (and the Google redirect URI) to match. |
| (B) public URL unreachable | The tunnel isn't up. ngrok path: did you use `--profile remote`? `docker compose --profile remote ps`, `docker compose logs ngrok` (authtoken + reserved domain set?). Own-proxy path: check it's listening and the cert is valid. The tunnel needs only app LIVENESS, so "app not installed yet" is never the cause. |
| Instance was installed, now refuses with a stage named | Honest degradation: a fact vanished (wiped volume, deleted seed, changed config). The status reports installed-then-degraded with the failed stage; close it again per its `closes_with`, then one `conventions` probe re-validates. A restored backup behaves the same — new proof is one tool call. |

---

*OAuth-only, Docker-based; local, remote, or cloud. The instance's state lives in FOUR
distinct places, and a machine move carries the first two: (1) the **Postgres volume** —
the graph, accounts, and install facts; back it up (`pg_dump`, or `scripts/backup-db.ps1`
on Windows); (2) **`.env`** — required configuration whose secrets only the human copies
or re-enters (they are gitignored and in no dump); (3) **`OPERATOR_OBSERVATIONS.md`** —
optional and local, moved by hand if wanted; (4) **reconstructible caches** (the
embedding model) — re-downloaded, never moved. Losing the database honestly returns the
service to "installing", never to a false "working"; losing `.env` returns it to the
config stage until the human refills the secrets.*

---

## Maintainer's note — acceptance runs

Before a release, the install process is proven by live acceptance runs on a fresh VM
(the project's own protocol, not a stranger's step). Each run leaves one record under
`docs/acceptance/`, shaped by `docs/acceptance/record.schema.json` (the allowlist IS
the schema, and it encodes the success conditions — a failed or empty run does not
validate) and validated by `scripts/validate_acceptance_record.py` — the validator
every in-VM export must pass before its one file crosses the VM boundary. **Mandatory
step before validation**: inside the VM, the composing agent derives a forbidden-name
list from the run's environment map (the host and machine names and database
names/identifiers it lists) and passes it as `--forbidden-file` — that list is the
enforcement for the name classes no static pattern can recognize; an empty list is
refused (a map always names at least the host and the database). The record's
`record_digest` is canonical (sha256 of the record with that field emptied, sorted-key
JSON) — the validator computes, checks, and prints it.

**The exact invocation, runnable in the stock environment** (only Docker and git exist
by contract; the app image already carries Python and `jsonschema`, and the repository
is present in the VM by construction — the run installed from it). From the repo root:

```bash
docker compose run --rm --volume "$PWD:/work" app \
  python /work/scripts/validate_acceptance_record.py /work/docs/acceptance/<run>.json \
  --forbidden-file /work/docs/acceptance/<run>.forbidden.txt
```

**[PowerShell]** equivalent (one line — PowerShell does not use backslash continuation):
```powershell
docker compose run --rm --volume "${PWD}:/work" app python /work/scripts/validate_acceptance_record.py /work/docs/acceptance/<run>.json --forbidden-file /work/docs/acceptance/<run>.forbidden.txt
```

(The script resolves its schema relative to its own path, so the whole-repo mount
suffices; nothing extra is baked into the image. The forbidden file itself never
crosses the VM boundary — only the validated record does. The `--forbidden-file` is
MANDATORY for the in-VM record — the validator refuses an exporting record without
it.)

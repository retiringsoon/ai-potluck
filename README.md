# ai-potluck

[![ci](https://github.com/retiringsoon/ai-potluck/actions/workflows/ci.yml/badge.svg)](https://github.com/retiringsoon/ai-potluck/actions/workflows/ci.yml)


## Why ai-potluck

Communities already share AI access informally — a friend's API key pasted into
a group chat, a team seat "borrowed" by five people, a subscription passed
around. It works until it doesn't: nobody can say who spent what, the person
lending carries all the risk, and every dispute ends at "trust me".

ai-potluck turns that handshake into infrastructure:

- **Contributors pool capacity, not credentials.** A contributor runs a
  connector on their own machine; upstream keys never leave it, and sharing is
  revocable at any moment.
- **Consumers pay per use, in the open.** Work is billed in milli-credits with
  a hard budget ceiling — no subscriptions, no deposits, no key sharing.
- **Every charge is provable.** Each answer produces a signed receipt and a
  transparency-log entry anyone can verify offline (`potluck verify receipt`) —
  and interrupted work is reconciled, not guessed.
- **The server keeps the books, not the power.** It routes, meters, and
  settles; it never sees an upstream credential, and clients can check its
  arithmetic against the public ledger.

Supply is invite-only and consent-based — see
[Supply compliance](#supply-compliance).


This repository is the client side: the `potluck` command, the connector, and
the protocol and verification core. Module layout follows one rule — the
protocol and verification primitives live here in `ai-potluck`, so a client can
verify a receipt without trusting the server; the server's bookkeeping and
gateway live in the companion repository
[`ai-potluck-server`](../ai-potluck-server) (`potluck-server`), which depends on
this package and imports these verification primitives to keep its own books.

*[中文说明](README.zh-CN.md)*

## Supported environments

The first batch is built, tested and supported on exactly these combinations:

| Target id | OS / architecture | CPython |
|---|---|---|
| `macos_arm64` | macOS 11 or newer on Apple Silicon (arm64) | 3.11 / 3.12 / 3.13 |
| `linux_x86_64` | Linux x86_64, glibc 2.17 or newer | 3.11 / 3.12 / 3.13 |
| `linux_arm64` | Linux arm64, glibc 2.17 or newer | 3.11 / 3.12 / 3.13 |

Explicitly **not** supported in this batch:

- Windows natively — use WSL2; the installer is POSIX `sh`.
- macOS on Intel (x86_64).
- CPython older than 3.11 or newer than 3.13. `pyproject.toml` allows `>=3.11`,
  which states what the code can run on; the matrix above states what a release
  is verified on, and `install.sh` refuses anything outside it.
- PyPy, GraalPy and other non-CPython interpreters.

The table lives in `scripts/support_matrix.py`, which is what `install.sh` checks
*before* it creates anything, and `tests/test_release_support_matrix.py` fails
when this section and that table disagree. An unsupported machine gets the matrix
plus the concrete next step, not a half-made install.

## Install

### One command (recommended)

You need [uv](https://docs.astral.sh/uv/) and a CPython from the matrix above.
From a checkout:

```bash
git clone <repo-url> && cd ai-potluck
./install.sh
```

`install.sh` checks this machine against the matrix, builds a self-contained
virtualenv under `~/.aipotluck`, installs the package into it, and prints the
version plus the next command. Add `~/.aipotluck/bin` to `PATH` when it asks you to.
Dependencies are resolved from the package index, so this step needs network
access; a failure prints uv's own output and exits non-zero rather than leaving a
half-installed prefix behind.

Variants:

```bash
./install.sh --python 3.11        # pin the CPython minor version
./install.sh --prefix ~/tools/potluck
./install.sh --preflight          # run the compatibility checks and stop
./install.sh --uninstall          # removes venv + launcher, keeps config/keys
./install.sh --source "ai-potluck @ git+https://HOST/REPO@v0.3.0"
```

An upgrade reruns the same command: the virtualenv is rebuilt from scratch (it
holds no user data), while `~/.aipotluck/config.json` and `~/.aipotluck/keys` — your
identity and connection settings — are never touched.

### From source (development)

```bash
cd ai-potluck
uv venv --python 3.13 .venv
uv pip install -e ".[dev]"
.venv/bin/potluck version
```

## Five minutes as a consumer

AI potluck is a shared table: contributors pool AI capacity they are allowed to
resell, and consumers spend it per question — no subscription, no key sharing.
Every answer carries a receipt: the exact token count, a cryptographic charge
record, and a transparency log you can verify yourself (`potluck verify
receipt`) instead of trusting anyone's spreadsheet.

Ask the operator for an invite code, then:

```bash
potluck join --server http://host:port --invite CODE
potluck ask "your question" --stream
```

`join` generates an Ed25519 key on this machine (mode 0600), redeems the code at
the server, and saves the identity to `~/.aipotluck` — later commands need no
further setup. Your invite carries a small milli-credit quota; `ask --stream`
prints each increment as it arrives. Real output looks like this (answer text
and numbers are illustrative):

```
potluck: looking for available nodes
potluck: generating
<the answer, printed as it is generated>
potluck: done
potluck: cost 38 milli-credit (38 tokens, receipt R-1)
```

The cost line names the tokens billed and the receipt that records the charge.
Without `--stream`, `ask` buffers: it waits, shows a `waiting for generation...` notice, then
prints the whole answer at once. Add `--json` for a single machine-readable
document.

Some invites also carry an API key for the server's OpenAI-compatible endpoint
(an operator mints those with `--with-api-key`). If yours did, `join` prints the
key once, on its own line, with the `base_url` it belongs to:

```
potluck: joined as consumer-1a2b3c (role consumer, tenant t-1a2b3c, key consumer-1a2b3c); identity saved to ~/.aipotluck
ak-Qm9…
potluck: this invite also issued an API key for the OpenAI-compatible endpoint (the key is the line above this one):
  base_url: http://host:port/openai/v1
  shown once; store it now — the server stored only its digest, so a lost key is
  replaced by the operator, never recovered
```

`join` does not write that key to a file: nothing this CLI runs needs one (`ask`
and `serve-openai` sign with the identity it already saved), so the only copy is
the one you store. Point a tool at that `base_url` with the key and every request
it makes spends your own account, per question, with a receipt behind it.

### Using it from your own tools

Two ways in, and they differ in what proves the spend was authorized.

**The server's OpenAI endpoint** — the drop-in path. The server serves
`POST /openai/v1/chat/completions` and `GET /openai/v1/models`, authenticated
with an API key for your consumer identity — one the operator minted for you, or
one your invite handed you when you ran `join`:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<server>/openai/v1",   # the server, not a provider
    api_key="ak-…",                          # potluck-server admin api-key
```

The server signs the terms it admits with its own settlement key
(`gateway_authenticated`), so a receipt from this path verifies honestly while
naming a weaker authorization than your own signature — the distinction the
docs keep rather than blur.

**Your own signing proxy** — the key-stays-home path. `potluck serve-openai`
runs a loopback endpoint (127.0.0.1 only) that speaks **both** shapes:

- `POST /v1/chat/completions` and `GET /v1/models` (OpenAI wire)
- `POST /v1/messages` (Anthropic wire — Claude Code works unmodified)

Every request is signed with this machine's `~/.aipotluck` identity; no server-
issued key exists to leak, and spending authority never leaves your hands —
symmetric with how a contributor's connector holds its own credentials.

```bash
potluck serve-openai            # http://127.0.0.1:8787/v1
potluck config emit --shell         # paste-ready OPENAI_BASE_URL/OPENAI_API_KEY
potluck config emit --claude-code   # paste-ready settings.json env block
```

**Contributors**: point a node at the upstream you already use —
`potluck contribute import-config --from-claude-code|--from-codex|--from-env`
reads the endpoint, model and key file from an existing tool's configuration,
writes `node.json` (key at mode 0600, never printed), and preserves everything
else already configured.

### Good to know

- **Pay per use, never per month**: the invite quota is the whole budget; the
  server declines work that would exceed it.
- **Interrupted answers are not silent charges**: if a node disappears
  mid-answer the attempt is marked *usage to be confirmed* and the server
  reconciles it — you are never billed for a number nobody can produce.
- **Your prompt travels**: the contributing node sees your prompt (it runs the
  model). Details in [Trust model](#trust-model) and
  [Supply compliance](#supply-compliance).

## Five minutes as a contributor

You supply capacity you are allowed to resell — see
[Supply compliance](#supply-compliance). Get a `node` invite from the operator,
then:

```bash
potluck join --node --server http://host:port --invite CODE \
  --upstream-endpoint https://api.example/v1/chat/completions \
  --upstream-model MODEL \
  --upstream-key-file ./key

potluck share run       # start sharing (foreground; Ctrl-C stops)
potluck share pause     # stop taking new work; in-flight work finishes
potluck share resume    # take new work again
potluck share status    # redacted local view
```

`--upstream-endpoint` must be the **full URL including the path** the upstream
accepts a chat-completions `POST` on (for example
`https://api.example/v1/chat/completions`), not just a base URL: the connector
posts to exactly the address you give it.

What happens on your machine:

- The upstream key is stored only in this machine's `node.json` (mode 0600). It
  is never uploaded and never printed.
- The setup self-check probes the upstream and the server over TCP only — it
  does not send your credential. `--probe-upstream-live` opts into one real,
  billable upstream request instead.
- Secrets are read from a file or an environment variable
  (`AIPOTLUCK_UPSTREAM_KEY`), never from a command-line argument, which every
  process on the host can read.

## Run your own server

A server is a single process over an embedded SQLite database — nothing else to
install. It ships in the companion repository `ai-potluck-server`, which depends
on this one. Deployment (TLS, service supervision, tunnels) is covered in
[docs/server-deployment.md](docs/server-deployment.md).

```bash
uv pip install ai-potluck-server   # or: -e <ai-potluck-server checkout>
potluck-server serve --config server.json
```

Mint invite codes on the server machine, where the store file lives:

```bash
potluck-server admin invite --config server.json --role consumer --tenant main
potluck-server admin invite --config server.json --role node --tenant main
potluck-server admin invite --config server.json --role consumer --with-api-key --quota 500000
```

A code is printed once; only its digest is stored. `--with-api-key` makes the
`join` that redeems the code hand the joiner an API key for the OpenAI endpoint
as well: minted at that moment and not before, capped by `--quota`, revocable on
its own, and shown once by `potluck join`.

## Trust model

- A settled request yields a receipt over the contributing device's signed usage
  report, appended to a Merkle transparency log — both can be verified offline.
- Every usage record today is at evidence level `node_reported`: a signature
  proves *who reported the numbers*, not that the upstream really consumed them.
- A contributor runs inference on their own machine and can see every prompt
  that flows through it.
- A tenant is a trust group for reading receipts: `GET /v1/receipts/{id}` answers
  any consumer credential in the receipt's tenant, its postings included, so
  consumers whose books must stay apart belong in separate tenants. A node's own
  dispatch history is scoped tighter — to the device its credential is bound to.

## Check your own records

Three read-only commands let each side confirm the others did not quietly lose or
edit a record. None of them writes anything and none freezes an account: they
report a difference, name the record, and leave the decision to you. None of them
creates a database either — a `database_path` that names nothing exits 1 and says
so, because an empty database holds no rows and would otherwise read as "no
differences" over a store that is simply gone.

```bash
# Consumer — verify one receipt over HTTP with the identity `join` saved
# (the config dir defaults to ~/.aipotluck).
potluck verify receipt --receipt-id R-1
potluck verify receipt --receipt-id R-1 --json

# Operator — the same check against the server's own database, which also holds
# the signed usage report, so all five dimensions are assessable.
potluck-server verify receipt --config server.json --receipt-id R-1

# Contributor — diff node.db against the attempts the server dispatched here
# (the node config defaults to ~/.aipotluck/node.json).
potluck share reconcile

# Operator — cross-check attempts, settlements, receipts, transparency-log
# leaves and the ledger replay log against each other, in both directions.
# --config is required: this command reads a server's own database.
potluck-server admin reconcile --config server.json
```

`verify receipt` reports five dimensions separately — `signature_valid`,
`metering_provenance`, `allocation_valid`, `ledger_included` and
`account_history_complete` — because they fail independently, and a single green
light would otherwise read as proof the upstream really consumed the tokens.
`all_pass` is printed as a summary of the five, never as a sixth verdict.
`unknown` means no evidence either way rather than a contradiction, so it does not
fail the command: from the consumer side `metering_provenance` is always
`unknown`, because a consumer holds no signed usage report. Both reconcile
commands exit 0 when the records agree and 1 when any difference stands.

A consumer reading over HTTP learns the platform's settlement public key from the
same server that serves the receipt, which detects a lost or reordered log but
not a server that rewrote history and re-signed it. Pin `public_key_b64` from
`GET /v1/checkpoint` out of band to close that gap.

## Behind a proxy

Outbound dials (ask/request to the server, the node-link WebSocket, and the
node's upstream call) honor the standard proxy variables:

```bash
HTTPS_PROXY=http://127.0.0.1:7897 potluck ask "hello" --stream
```

- `https`/`wss` destinations read `HTTPS_PROXY`, then `ALL_PROXY`; `http`/`ws`
  destinations read `HTTP_PROXY`, then `ALL_PROXY`
- both HTTP CONNECT proxies (`http://`, `https://`) and SOCKS5
  (`socks5://`, remote DNS) are supported, with optional `user:pass@` auth
- `NO_PROXY` excludes destinations (`*`, exact hosts, suffix domains); the
  loopback addresses `localhost`, `127.0.0.0/8`, and `::1` are always direct

## Supply compliance

The project supports only L1 whitelist supply: your own API key, a self-hosted
model, or enterprise quota you are contractually allowed to resell. Sharing a
personal monthly subscription usually violates the upstream's terms of service;
the project does not support that by default, and you are responsible for how
you use it.

## Current limitations

- Invite-only onboarding.
- No top-up and no withdrawal: credits are minted by the operator and cannot be
  cashed out.
- One server, one instance — no federation or high availability.
- Only the environments in [Supported environments](#supported-environments);
  Windows is not supported natively, so use WSL2.

## Development

```bash
cd ai-potluck
uv venv --python 3.13 .venv
uv pip install -e ".[dev]"

.venv/bin/python -m pytest tests/ -q --basetemp=.pytest_basetemp
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
```

The end-to-end release gate — a real install plus the default join/ask path
against a real server — lives in the `ai-potluck-server` repository
(`scripts/release_gate.sh`), because it drives both packages.

`--basetemp` is only needed in sandboxes that block pytest from creating its
temporary directory under the system temp path; a normal machine does not need
it. `mypy` runs with `--strict` (configured in `pyproject.toml`) and must stay
at zero warnings.

## Docs

- [Proposal & early-user asks (RFC)](docs/PROPOSAL.md) ([中文](docs/PROPOSAL.zh-CN.md))
- [Roadmap](docs/ROADMAP.md)
- [Network contract v0.1](docs/network-contract-v0.1.md)
- [Server deployment](docs/server-deployment.md)

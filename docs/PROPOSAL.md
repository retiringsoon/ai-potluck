# potluck — pool AI quota with people you actually trust

> **Status: seeking early users.** This is a request for comments *and* a request for help: read the
> idea, kick the tires, and tell us what would make this genuinely useful to you (see
> [What we're asking](#what-were-asking-early-users)). Chinese version: [PROPOSAL.zh-CN.md](PROPOSAL.zh-CN.md).

## The problem

Access to AI models is unevenly distributed: one person has enterprise API
credits they never burn through, another runs a local GPU with vLLM, a third just wants an
occasionally good model without opening five accounts. Pooling works — **until the keys and the
money show up**:

- Hand-copying API keys around is a security incident waiting to happen.
- "I'll Venmo you later" doesn't scale past three people, and nobody trusts a spreadsheet.
- When something goes wrong — wrong numbers, a flaky node, a disputed charge — there is no
  shared source of truth to look at.

Existing gateways solve the *aggregation* problem (one key in front of many models). They do not
solve the *trust between parties* problem, because they assume one operator owns everything.

## The idea

**potluck is the trust layer for small AI-sharing groups.** The group runs one small center;
members pick a side:

- **Contributors** run a connector on their own machine and share one upstream
  (an API endpoint or a self-hosted model). **Upstream credentials never leave that machine** —
  they live in a `0600` local file and are never uploaded, printed, or proxied.
- **Consumers** join with an invite code, then `potluck ask "..."` — they get a streaming answer
  and a cost line with a receipt id.

Every usage report is **signed by the device** that did the work; every settlement is **signed by
the center** and anchored into an append-only **Merkle transparency log**. Anyone holding a
receipt can verify it offline — signature, metering provenance, allocation math, ledger
inclusion, account history — and every record reconciles across sides with
`share reconcile` / `admin reconcile`.

## How a request actually travels

Three programs, three network roles. **Only the center listens**; nodes and consumers dial out —
home NAT and firewalls are fine, no port forwarding anywhere.

```text
                   ┌───────────────────────────┐
                   │  center — one small box   │
                   │  identity registry, invites│
                   │  credit ledger + reserves  │
                   │  dispatch, content relay   │
                   │  settlement, receipts,     │
                   │  transparency log          │
                   └──────▲─────────────▲──────┘
         outbound WSS     │             │   HTTPS + SSE
         (node dials out; │             │   (signed requests)
          no inbound port)│             │
         ┌────────────────┴───┐   ┌─────┴────────┐
         │ contributor machine │   │ consumer     │
         │ · holds the API key │   │ · signs every│
         │   (0600, local only)│   │   request    │
         │ · calls the upstream│   └──────────────┘
         └─────────┬───────────┘
                   │ TLS
         ┌─────────▼───────────┐
         │ upstream (API or    │
         │ self-hosted model)  │
         └─────────────────────┘
```

One `ask`, end to end:

1. **Sign.** Your CLI builds the request (prompt, model, charge ceiling, expiry) and signs a
   spend authorization with your Ed25519 key: it names *this* request, *your* account, and the
   maximum you accept. The signature travels in the header; your private key never moves.
2. **Reserve.** The center verifies the signature, checks your balance, and **reserves** the
   ceiling — it cannot move that money twice.
3. **Offer.** The center picks an online node that serves the model and pushes an offer down
   the node's already-open outbound link. The node checks the terms against its **local** caps
   — it can refuse, and the center cannot raise those caps.
4. **Execute.** The node calls the upstream **directly from its own machine, with its own key**.
   Text increments stream back through the center to your terminal while the model is still
   writing.
5. **Report & settle.** The node signs a usage report; the center verifies both signatures and
   the ceiling, then books the split: your reserve → the contributor's account. You get a
   receipt id; the receipt is anchored in the transparency log.
6. **When things break.** No node online → your CLI says so within seconds and creates no
   request at all. The node dies mid-work → retry elsewhere or fail with the named reason;
   usage that can't be determined is marked *pending review*, never silently billed as zero.
   Ctrl-C → a real end-to-end cancel that reaches the in-flight attempt.

### What the center does — and what it can't

| The center… | |
|---|---|
| **holds** | the identity/key registry, invite codes, the credit ledger + reservations, the transparency log |
| **does** | verifies signatures, dispatches offers, relays stream chunks, settles, issues signed receipts, serves audit endpoints |
| **never holds** | upstream API keys — they exist only on contributor machines |
| **never does** | calls an upstream itself; moves credits without your signed per-request authorization; raises a node's local caps |

The center is the *accountable* party, not an all-powerful one. It can't forge a contributor's
usage report (device signature), and it can't spend your balance without your per-request
signature — but it does run the bookkeeping, and it holds the settlement key. That's exactly
why every settlement is signed and anchored in the append-only transparency log, and why the
audit commands (`verify receipt`, `share reconcile`, `admin reconcile`) verify it offline:
bookkeeping that doesn't reconcile is detectable, by name.

### Who can see what

| Party | Sees your prompts? | Sees upstream keys? | Can move your credits? |
|---|---|---|---|
| Center operator | yes — relays and stores requests | no | only within your signed per-request ceiling |
| Contributor node | yes — it executes the model | only their own | only earns, per settled receipt |
| Upstream provider | the prompt (they serve it) | — | no |

**Retention default (planned)**: the center's permanent records are billing data — usage,
amounts, model, signatures — plus content hashes. Prompts are stored as hashes only, and
delivered output is purged after a dispute window; keeping content for training would be an
explicit opt-in on both sides (contributor disclosure + consumer consent), never a silent
default. Receipts and reconciliation stay hash-based either way.

**Network practicalities**: the center must be reachable by all members — a small public VPS, a
Tailscale network, or a LAN all work. Nodes and consumers only make *outbound* connections. If
the center runs TLS (built-in certfile/keyfile or your reverse proxy), every hop of the prompt
is encrypted: consumer → center → node → upstream. Wire-level detail: the network contract
linked from the [README](../README.md#docs).

## What works today (v0.1.0)

This is a working system, not a whitepaper. Concretely:

- **Invite onboarding** — the operator mints invite codes (role, quota, expiry, use limits).
  Consumers and contributors join with one command; keys are generated locally; identities are
  derived server-side; no JSON editing, no token copying. Re-joining is idempotent.
- **`ask`** — streaming (`--stream`) or buffered answers; the model is inferred from online
  nodes; failures tell you *why* (e.g. "no node can take the work: budget 20000 > node's local
  exposure 5000") and next steps; Ctrl-C is a real end-to-end cancel.
- **Contributing** — `share setup` walks through upstream endpoint/key/model with a self-check
  that never sends your key; `share run/pause/resume/status` manage availability; conservative
  local caps (concurrency, in-flight exposure) are enforced locally and cannot be raised by the
  center.
- **Receipts & audit** — `potluck verify receipt` runs a five-dimension offline verification;
  `potluck share reconcile` and `potluck-server admin reconcile` diff the node's local signed records
  against the center and name the exact mismatched record. All three are read-only and refuse to
  open a database that doesn't exist.
- **Center** — a single process with embedded SQLite (no external database), one port for
  HTTP/WebSocket/SSE, TLS built in or terminated at your reverse proxy; deployment guide with
  systemd/launchd/backup runbook. Restarts recover state from disk; crash windows are tested.
- **Release hygiene** — every release bundle passes an automated gate: clean tree, offline wheel
  install into a fresh venv, and a real end-to-end smoke (center + node + consumer) before the
  tarball is produced. `RELEASE_INFO` inside the bundle pins the exact commit.

Everything above is covered by ~1900 tests, `ruff`, and `mypy --strict` (zero findings).

## Design principles

1. **Credentials never leave the machine that earned them.** The center routes work; it never
   holds upstream keys.
2. **Signatures prove who reported — not what is true.** Every usage number carries an evidence
   level. Today everything is `node_reported` (self-reported by the contributor); the receipts
   make reporting *attributable* and *auditable*, not omnipotent. Higher evidence
   (platform-observed traffic, attested metering) is a roadmap decision, not a pretense.
3. **Local limits are final.** The center cannot raise a contributor's concurrency or exposure
   caps; a node refuses offers its operator didn't sign up for.
4. **Small by construction.** One Python process, embedded SQLite, one port. A community center
   should be deployable on a $5 VPS and understandable in an afternoon.
5. **Invite-first.** This is built for groups of people who know each other. Strangers-scale
   economics (and their attack surface) are explicitly out of scope until the trust mechanisms
   earn them.
6. **Fail closed.** Missing data is an error with a next step, never a silent success.

## What this is not

- **Not a strangers' marketplace.** No public listing, no open enrollment.
- **Not a token economy.** Credits are an internal accounting unit; there is no crypto, no
   speculation, no custody of real money.
- **Not anonymous.** Contributors execute your prompts on their machines and can read them.
  Share with people you trust; we say so in the docs instead of pretending otherwise.
- **Not a ToS workaround.** Only L1 supply is supported: your own API keys, self-hosted models,
  or quota you're entitled to share. Sharing personal subscription seats usually violates
  upstream terms of service — we don't support it and won't ship features designed for it.

## Roadmap

Full version: [ROADMAP.md](ROADMAP.md). Short version:

- **Shipped (v0.1.0):** everything under "What works today".
- **Shipped since v0.1.0:** **OpenAI-compatible API access** (see below) — any existing tool
  consumes the pool directly with a center-issued API key.
- **Next (committed):** a local signing proxy for that surface; tool calling over it; extract to a
  standalone public repo; CLI output internationalization (strings are currently Chinese);
  multiple models per node; a Windows story beyond WSL; a hardening batch (invite-consumption
  ordering, revocation semantics, a cosmetic status flicker).
- **Considering — your feedback decides:** credit top-up flows, per-model pricing, automated
  dispute flow, a platform-observed metering proxy, TEE-attested metering, x402-style per-call
  payments, a Docker image, a minimal local chat UI.

### Shipped: use it from any tool (OpenAI-compatible API)

`ask` proves the pipeline; for real work you shouldn't have to leave your tools. The center now
serves an **OpenAI-compatible endpoint** alongside its own API:

```text
base_url = https://<your-center>/openai/v1
api_key  = ak-…   (potluck-server admin api-key)
model    = <whatever your group's nodes serve>
```

- The center issues **revocable API keys bound to an existing consumer, each with a credit cap**,
  plus a per-key concurrency ceiling and request rate. Every tool request is authorized
  automatically *within that cap* — same reservation, settlement, signed receipts and
  transparency log as a signed request; nothing about the audit story changes, and the key's
  spend is counted against its quota in the same transaction that records the settlement.
- **Multi-turn works, and it is the point**: the internal request model carries the conversation
  itself rather than a flattened prompt, so every turn reaches the contributor's upstream
  verbatim together with the parameters the platform forwards. Each request is one authorized
  spend, and per-key/per-request costs stay reconcilable.
- Honest limits: no embeddings, no legacy completions endpoint, one text choice (`n > 1` is
  ignored) and **no tool calling** — `tools`/`tool_choice` are not forwarded, so an agent
  framework that drives a conversation through tool calls needs the signed `/v1/*` path. Those
  refusals are explicit (`404`, `error.type: not_supported`) rather than silent.
- Honest tradeoff: with a center-issued key, the center signs the per-request authorization
  inside your cap with the platform settlement key, labelled `auth_method:
  gateway_authenticated`. You extend it bounded spending authority, revocable at any moment, and
  `verify receipt` reports such a receipt truthfully — as platform-authorized, **not** as a
  consumer signature. For those who want signing to stay client-side (design principle #1,
  symmetric with the contributor connector), a small **local signing proxy** is still planned.

## What we're asking early users

Answer any subset, in an issue or a discussion — one-liners are fine:

1. **Supply:** what would you share — an OpenAI-compatible API, Anthropic, a local `ollama` /
   `vLLM` server, something else? What's the endpoint shape we *must* support?
2. **Consumption:** API access is now the **committed next milestone** (see
   *Coming next* above) — tell us which tools you'll point at it (IDE plugins, agent frameworks,
   scripts, chat UIs) and what they need (streaming, tool calls, model listing, prompt-caching
   headers).
3. **Economics:** in your group, how should credits work? Admin tops everyone up? Contributors
   earn credits they spend back? Per-model pricing? Caps per person per day?
4. **Platforms:** what does your community actually run (macOS / Linux / Windows / Docker)?
   Is a Docker image for the center a must-have?
5. **Trust:** does "the contributor can read prompts" fly with your community? What mitigation would
   change your answer (prompt redaction, opt-in local-only mode, evidence upgrades)?
6. **The one thing:** what single missing capability would make you use this every week?

## How to participate

- **Install:** grab the release bundle, unpack, `./install.sh` (macOS/Linux, Python 3.11+;
  offline install works — the bundle carries its dependency wheels). Then follow
  [README](../README.md): one command to join, one command to ask, one command to share.
- **Report:** open an issue with the
  [feature request](../../issues/new?template=feature_request.md) or
  [bug report](../../issues/new?template=bug_report.md) templates (`potluck doctor` gives you a
  redacted diagnostic to attach). Chinese is welcome everywhere.
- **Read the details:** network contract, transport ADR, protocol background, and an
  engineering review with all known warts are linked from the
  [README docs section](../README.md#docs).

## License

MIT — see [LICENSE](../LICENSE).

# AGENTS.md — ai-potluck

Entry point for AI agents working in this repository. Read this before editing
code; [CONTRIBUTING.md](CONTRIBUTING.md) has the human workflow, and its rules
apply to agents too.

## What this repo is

ai-potluck is the client side of a shared-AI-access network: the `potluck`
CLI (consumer + contributor), the connector process, and the protocol and
verification core. It makes every charge provable — signed receipts, an
integer credit ledger, and an append-only transparency log that anyone can
re-verify offline.

**Companion repo:** [ai-potluck-server](https://github.com/retiringsoon/ai-potluck-server)
is the center (gateway + settlement books + operator CLI). Dependency
direction is one-way: **verification primitives live here in the client; the
server depends on this package and imports them to keep its own books**, so a
client can verify a receipt without trusting the center. Never make the
client import from the server.

## Layout map (`src/aipotluck/`)

| Module | Purpose |
| --- | --- |
| `canonical.py` | RFC 8785 JSON canonicalization + domain-separated Ed25519 signing |
| `records.py` | The four signed record types forming the auditable chain |
| `identity.py` | Identity keys, key delegation, and signing roles |
| `metering.py` | Token measurement: normalization, dedup, coverage/evidence levels |
| `allocation.py` | Deterministic integer allocation across beneficiaries |
| `ledger.py` | Integer credit ledger: reservations, paired journals, settlement |
| `settlement.py` | Settlement orchestration: signed reports → signed receipt |
| `reproducible.py` | Optional reproducible billing basis both parties can recompute |
| `transparency.py` | Append-only Merkle transparency log with signed checkpoints |
| `verify.py` | Offline verification: independently recompute a party's records |
| `reconcile.py` | Two-sided reconciliation, consumer and contributor side |
| `statemachine.py` | Request / attempt / output / settlement state machines |
| `coordinator.py` | Dispatch, failover, output-commit race; the only layer that retries |
| `connector.py` | Contribution-node connector over one outbound WebSocket |
| `adapters.py` | Fixed upstream adapters (exactly one per contribution node) |
| `wire.py` | Strictly parsed JSON envelopes for both network links |
| `websocket.py` | Minimal RFC 6455 WebSocket transport |
| `proxy.py` | Outbound proxy support for every dial this package makes |
| `tls.py` | Client TLS defaults, honoring a privately deployed CA |
| `persistence.py` | Durable node store; reconnect never re-executes or re-bills |
| `deploy.py` | Config schemas, node/consumer loading, consumer HTTP client |
| `cli.py` | `potluck` entry point; `__main__.py` mirrors it |
| `errors.py` | Domain exceptions, each with an actionable message |
| `ux.py` | Human-facing wording for machine states, in exactly one place |
| `fake_upstream.py` | Controllable fake upstream for exercising abnormal paths |

Also: `tests/` (behavior-level suite), `scripts/` (`acceptance_kit.sh`,
`dev_fake_upstream.py`, `support_matrix.py`), `docs/` (proposal, roadmap,
network contract, center deployment), `install.sh` (user installer).

## Build / test / verify

```bash
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"
python -m pytest tests/ -q --basetemp=.pytest_bt   # --basetemp is required locally
ruff check .
mypy                                                # strict
```

**Verification is evidence-based.** Never claim green without running the
command; a change is complete only when ruff, mypy, and the full pytest suite
all pass. The cross-repo end-to-end gate (`scripts/release_gate.sh`) lives in
ai-potluck-server.

## Invariants (do not break)

1. **Signing semantics.** Ed25519 domain separation and canonical JSON
   (RFC 8785) are the trust root. Changing a domain separator, canonicalization
   rule, or signed field set breaks every existing receipt — requires an
   explicit, reviewed protocol change, never a drive-by edit.
2. **The trust core is tested behaviorally.** Metering, allocation,
   settlement, the ledger, and the transparency log: any change must ship
   behavior-level tests (observable outcomes — balances, receipts, inclusion
   proofs — not implementation details).
3. **Fail loud.** Config loading and initialization must never swallow
   errors: reject unknown config keys, raise on missing required fields, and
   never paper over problems with `config.get(key, default)`. A wrong config
   must fail at startup with an actionable message, not misbehave at runtime.
4. **Forward-only.** No compatibility shims, no dual code paths. Deleting an
   old path and updating all its callers happens in the same change set. The
   only exception is persisted data formats, which need a migration script.

## Agent hygiene

- Docs and code speak the same language: English.
- Never introduce absolute local paths, machine names, credentials, or other
  private/environment-specific information into committed files.
- Keep changes minimal and self-consistent: no drive-by refactors, no
  speculative scaffolding, no leftover TODOs or dead code.

# Roadmap

> Living document. Priorities below "Shipped" move in response to early-user feedback
> ([PROPOSAL.md](PROPOSAL.md#what-were-asking-early-users)) — that's deliberate.
> Status as of **v0.1.0**.

## ✅ Shipped — v0.1.0

- **Protocol core**: Ed25519 domain-separated identities, canonical JSON (JCS/RFC 8785),
  double-signed usage receipts (device + platform), deterministic integer allocation,
  append-only Merkle transparency log with inclusion/consistency proofs.
- **Invite onboarding**: one-command join for consumers and contributors; server-derived
  identities; invite quotas/expiry/use-limits; self-provisioned node credentials (no token
  copying, no manual JSON).
- **Ask**: streaming (`--stream`) and buffered modes; model inferred from online nodes; failure
  causes and next steps surfaced; end-to-end cancel.
- **Contribute**: setup wizard with credential-safe self-check; run/pause/resume/status; local
  concurrency and exposure caps that the center cannot raise.
- **Audit**: five-dimension offline receipt verification (`verify receipt`); bilateral
  reconciliation (`share reconcile`, `admin reconcile`); tamper-evident with named-record
  reporting; read-only and fail-closed on missing databases.
- **Center**: single process, embedded SQLite, one port (HTTP+WS+SSE), built-in TLS or reverse
  proxy, restart recovery, deployment guide (systemd/launchd/backup/key handling).
- **Release hygiene**: offline-install bundle with an automated build gate (clean tree + fresh
  venv + real end-to-end smoke + `RELEASE_INFO` commit stamp).
- **Tests/docs**: ~1900 tests, `ruff`, `mypy --strict` clean; quick-start README (EN/中文),
  network contract, transport ADR, deployment guide, proposal (this release's RFC).

> **Release blockers first (2026-10-10):** follow [发布前修复清单](release-fix-plan.zh-CN.md) F01–F06 before expanding scope. Node identity binding and credential revocation are P0 blockers, not optional hardening.

## 🔧 Next — committed

| Item | Why |
|---|---|
| Standalone public repo + first GitHub release | publish from a standalone repository with a clean, rebuilt history |
| CLI output internationalization (EN) | all CLI strings are Chinese today; the docs are already bilingual |
| **OpenAI-compatible endpoint mode (confirmed next milestone)** | any OpenAI-compatible tool works against the pool: center-issued revocable API keys with per-key credit caps, auto-authorized within the existing reservation/settlement path (receipts + transparency log unchanged); a local signing proxy (keys stay client-side, symmetric with the contributor connector) planned alongside; unlocks long-running agent loops with zero potluck-specific config; ships with config import (contributors: parse existing Claude Code settings.json / codex config.toml / env vars for upstream endpoint+model+key) and config emitters (consumers: a ready-to-paste Claude Code env block or plain base_url + api_key) |
| Multiple models per node | one node currently shares exactly one upstream model |
| Windows story | official WSL guide today; evaluating a PowerShell installer |
| Hardening batch | invite-consumption ordering on late failures; revocation semantics for minted credentials; a cosmetic streaming status flicker; hello subject cross-check |
| Contribution infrastructure | CONTRIBUTING.md, SECURITY.md, issue triage process |

## 🗳 Considering — feedback decides

| Candidate | Open question for users |
|---|---|
| Credit top-ups / recharge flows | how do real groups fund the pool? |
| Per-model pricing & rate variants | flat milli-credit-per-token vs per-model multipliers |
| Automated dispute flow | freeze-on-discrepancy → evidence packs → arbitration; how much should be automatic? |
| Platform-observed metering (`platform_observed` evidence) | would you accept a traffic-visible proxy for stronger guarantees? |
| Attested metering (`attested_meter`, TEE) | worth it only with compatible hardware supply — tell us if you have it |
| x402-style per-call payments | machine-payable usage without accounts |
| Docker image for the center | must-have or nice-to-have? |
| Web chat UI | a small hosted page that talks to the OpenAI-compatible endpoint — the friendly answer for non-CLI members; CLI `ask` stays as the plumbing/test path |
| Agent session support | long-running agent loops: session-level cost aggregation, sticky node preference, tuned long-task defaults (deadline/timeout) — pairs with the OpenAI-compatible endpoint mode; agent context stays client-side, each turn is one authorized request |
| Dispatch queueing / bidding | peak-vs-trough concurrency: today there is a client grace-polling wait but no server-side queue (first-come-first-served); interim backstop needs zero code — the operator runs a reserve node with a high `max_concurrency`; server-side FIFO or credit-bid-weighted ordering only if users actually feel contention |
| Performance batch (deferred) | delta batching + WAL `synchronous=NORMAL` (write-path headroom for 100+-user scale) and a 500-stream load-test harness — explicitly deferred until the community tier shows real usage |
| Retention modes | privacy-first default: billing records kept forever, prompts stored as hashes only, delivered output purged after a dispute window; training-data retention only as explicit dual-sided opt-in (contributor disclosure + consumer consent). Receipts and reconciliation stay hash-based either way |
| PyPI publication (deferred) | distribution is source-first: one-command install from git or checkout (uv/pip/pipx) — no bundles, no PyPI for now; both channels stay revocable/deferrable, and PyPI (immutable, yank-only) is revisited only after the API stabilizes with CI secret-scanning/pre-publish checks |

## 🚫 Won't do (for now, on purpose)

- Token/currency incentives and speculation
- A public strangers' marketplace with open enrollment
- Custody of real money or withdrawal promises
- Anonymous supply or features whose purpose is bypassing upstream terms of service
- Federated/P2P trust (the center is deliberately simple and accountable)

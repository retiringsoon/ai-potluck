# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

Only the latest release line on `main` receives security fixes. Fixes land on
`dev` first and reach `main` through the normal release flow.

## Reporting a vulnerability

Use **GitHub Private Vulnerability Reporting** — do not open a public issue:

<https://github.com/retiringsoon/ai-potluck/security/advisories/new>

Include: affected version, steps to reproduce, impact, and whether the report
also affects [ai-potluck-server](https://github.com/retiringsoon/ai-potluck-server)
(file it there too if so; cross-repo fixes are coordinated through both advisories).

## Response expectations

- Acknowledgement within 3 business days.
- Triage and an initial severity assessment within 7 business days.
- We aim to ship a fix or mitigation before public disclosure, and to agree on
  a disclosure timeline with you (default: 90 days).

## Scope

Highest priority — the trust core of the protocol:

- Ed25519 domain-separated signing and canonical JSON (RFC 8785) semantics
- Token metering, allocation, and settlement arithmetic
- Receipt verification and offline re-verification (`potluck verify`)
- Transparency log inclusion and checkpoint validation
- Identity, key delegation, and invite handling

## Out of scope

- Denial-of-service reports (resource exhaustion, flood attacks)
- Social engineering of operators or contributors
- Attacks requiring physical access to a participant's machine

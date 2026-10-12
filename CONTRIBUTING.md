# Contributing to ai-potluck

Thanks for helping make shared AI access provable. This repo is the client
side: the `potluck` CLI, the connector, and the protocol/verification core.
The center (gateway + books) lives in
[ai-potluck-server](https://github.com/retiringsoon/ai-potluck-server).

## Branching and dev workflow

- `dev` is the integration branch. All feature work lands here first.
- `main` is the release surface. It only ever receives verified merges from
  `dev` — never direct feature commits.
- Create your feature branch from `dev`:

  ```bash
  git checkout dev && git pull
  git checkout -b feat/your-change
  ```

- When the change is complete, open a pull request **into `dev`**.

## Local development setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"
```

### Tests

```bash
python -m pytest tests/ -q --basetemp=.pytest_bt
```

`--basetemp` is **required** for local runs: the sandboxed environments many
contributors use cannot write to the system temp directory. CI omits it
because runner temp space is writable.

### Lint and types

```bash
ruff check .
mypy
```

`mypy` runs in strict mode over `src/aipotluck` (configured in
`pyproject.toml`). A change is done only when ruff, mypy, and the full pytest
suite are all green — see [AGENTS.md](AGENTS.md) for the evidence rule.

### End-to-end gate

The full install-and-run gate (`scripts/release_gate.sh`) lives in the
[ai-potluck-server](https://github.com/retiringsoon/ai-potluck-server) repo,
because it exercises both packages through a real center. Run it before
calling a cross-cutting change releasable.

## Commit discipline

- **One commit = one user-visible value.** Prefix by kind: `feat:`, `fix:`,
  `docs:`, `test:`, `refactor:`. If you cannot state the user-visible value
  in the subject line, split or squash.
- Commit messages describe *what changed and why*, not the process. No
  session diaries ("after debugging we found…"), no absolute local paths, no
  throwaway branch names.
- Run the test suite before committing. Do not commit red.

## Pull request standards

- **Behavior-level tests.** Test what a user or a counterparty can observe —
  signed receipts, ledger balances, CLI output — not implementation details.
  Every fix ships with a test that fails before the fix and passes after.
- **All green.** ruff, mypy (strict), and the full pytest suite pass in CI
  before review.
- **Docs in the same PR.** If the change affects commands, configuration, or
  the network contract, update `README.md` / `docs/` in the same pull
  request — not a follow-up.
- Keep PRs small and coherent. A PR that mixes an unrelated refactor with a
  behavior change will be asked to split.

## Where to look next

- [AGENTS.md](AGENTS.md) — repo invariants (signing, metering, fail-loud
  config, forward-only) that every change must respect.
- [SECURITY.md](SECURITY.md) — how to report a vulnerability privately.
- [docs/network-contract-v0.1.md](docs/network-contract-v0.1.md) — the wire
  contract between clients and a center.
- Questions or ideas before opening an issue:
  [Discussions](https://github.com/retiringsoon/ai-potluck/discussions).

"""Command-line entry point for the consumer and contributor side of AI potluck.

The operator commands that open a server's own store or run one — ``serve``,
``admin invite``/``invite-list``/``revoke-key``/``reconcile``, and the operator
view of ``verify receipt`` — live in the ``ai-potluck-server`` package's
``potluck-server`` command.

Offline operations:

* ``keygen`` writes an Ed25519 private key to disk (mode 0600) and prints the
  matching public key.
* ``sign`` reads a JSON payload and a private key, and prints the
  domain-separated base64 signature.
* ``verify`` checks a signature over a payload and prints a verdict.
  ``verify receipt`` runs :mod:`aipotluck.verify`'s five-dimension offline
  verification over one receipt instead, against the three documents a consumer
  can fetch with ``--config-dir``.

Reconciliation (read-only; nothing is written and no account is frozen):

* ``share reconcile`` diffs this node's ``node.db`` against the attempts the
  server says it dispatched here, and names every attempt the two sides
  disagree about.

Contributor configuration (nothing is uploaded; an upstream key stays here):

* ``contribute import-config`` reads the upstream endpoint, model and API key out
  of a configuration that already exists on this machine — Claude Code's
  ``settings.json``, Codex's ``config.toml``, or the ``OPENAI_*``/``ANTHROPIC_*``
  environment — and writes them into ``node.json``'s adapter block, so a
  credential is not retyped into a second place where it can go stale. A field the
  source does not name is reported by the key path it was looked for, together
  with the argument that would supply it, and an incomplete import writes
  nothing. See :mod:`aipotluck.configimport`.

Network operations (wired through :mod:`aipotluck.deploy`):

* ``connect`` runs a contribution node from ``node.json``, probing the
  configured upstream first so blocked egress is learned before the server
  dispatches to the node.
* ``request`` submits a consumer request that must carry a signed spend
  authorization (produced with ``sign`` plus the consumer key), optionally
  waiting for settlement.
* ``status`` prints a request's status document as canonical JSON.
* ``events`` reads a request's stored events off the gateway's SSE stream.

Onboarding and consumer commands (the invite flow):

* ``join`` generates an Ed25519 key (mode 0600), redeems an invite at
  ``POST /v1/join``, and writes ``~/.aipotluck/config.json`` so later commands
  need no further setup. A node join also receives the platform link token the
  server minted for it — returned once, stored in ``node_token.json`` (0600),
  never printed — so the contributor wizard needs no token an operator had to
  copy over by hand. A consumer join on an invite minted ``--with-api-key``
  receives an API key for the server's OpenAI-compatible endpoint instead:
  printed once, on its own line, beside the ``base_url`` it belongs to, and
  deliberately not written to a file this package would then have to protect.
* ``ask`` is the one-command consumer path: it signs a request with the joined
  identity, dispatches it, waits for the answer, and prints the text and a cost
  line. Without ``--model`` it asks the server which models the online nodes
  actually serve and requests the commonest one. A request that ends in a
  failure state prints the server's own ``billing_note`` verbatim, so "did not
  finish" is never the whole story. Answers are delivered in buffered mode today
  (the display is a replaceable function so a streaming worker can make it
  incremental).
* ``version`` prints the package and protocol versions.
* ``serve-openai`` runs a loopback-only HTTP endpoint that speaks the shapes
  tools already use — OpenAI's ``POST /v1/chat/completions`` and ``GET
  /v1/models``, and Anthropic's ``POST /v1/messages``, which is what Claude Code
  speaks — and turns each request into one on the same signed consumer path
  ``ask`` uses, so a tool needs a ``base_url`` and no potluck-specific code. The
  endpoint lives in :mod:`aipotluck.localapi` and the tool-facing wire shapes in
  :mod:`aipotluck.openai_wire`.
* ``config emit`` prints the configuration block that points a tool at that
  endpoint — ``--shell`` for an OpenAI-compatible client, whose base URL carries
  ``/v1``, and ``--claude-code`` for Claude Code's ``settings.json`` env block,
  whose base URL must not, because Claude Code appends ``/v1/messages`` itself.
  See :mod:`aipotluck.configemit`.

The payload is parsed with :func:`aipotluck.canonical.loads_strict` on both the
signing and verification path. That is deliberate: a payload the signer would
have refused (a float, a duplicate key, an integer past the interoperable safe
range) must fail identically at verification rather than being silently
reinterpreted.

``argparse`` is used rather than ``typer`` on purpose. The project keeps
``cryptography`` as its only runtime dependency, so pulling in a CLI framework
would break that constraint; the argument surface here is small enough that the
standard library is a fair trade.

Every error path prints an actionable message on stderr and returns a non-zero
exit status. The console script's generated wrapper calls ``sys.exit(main())``,
so the integer returned here becomes the process exit code. No command prints
a token, an invite code, a private key, or an upstream api_key: credentials live
in the config files and in request headers, and diagnostics go through the
redacted views in :mod:`aipotluck.deploy`.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import getpass
import http.client
import importlib.metadata
import json
import os
import secrets
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from aipotluck.tls import client_ssl_context

from . import canonical, configimport, identity, ux
from .adapters import HttpUpstreamAdapter, probe_endpoint
from .canonical import canonicalize
from .connector import Connector
from .deploy import (
    IDENTITY_CONFIG_KEYS,
    LiveEventStream,
    SignedClient,
    SseFrame,
    bearer_get,
    consumer_events,
    consumer_get,
    consumer_post,
    load_consumer,
    load_document,
    load_node,
    poll_request_until_terminal,
    redacted_node,
    reject_renamed_keys,
    reject_unknown_keys,
)
from .errors import CanonicalizationError, PotluckError, UnknownKeyError
from .identity import generate_key_pair, load_private_key
from .persistence import NodeStore
from .reconcile import (
    DIMENSIONS,
    Difference,
    NodeAttemptView,
    ReceiptVerification,
    reconcile_node,
    verify_receipt_from_server,
)
from .records import SpendAuthorization, new_nonce
from .statemachine import PRE_DISPATCH_REQUEST_STATES, TERMINAL_REQUEST_STATES
from .wire import ChatMessage, check_session_id, compute_request_digest, user_message

PROG = "potluck"

#: The installed distribution ``_package_version`` reads. Not ``PROG``: the
#: command name and the package name differ.
DIST_NAME = "ai-potluck"

#: The frozen wire/schema contract version this build speaks. Printed by
#: ``potluck version`` alongside the package version.
PROTOCOL_VERSION = "v0.1"

#: The protocol version to report when the installed distribution metadata is
#: unavailable (for example when running from a source tree without an install).
_FALLBACK_VERSION = "0.0.0"

#: Domain the consumer signs spend authorizations under. The ``request``
#: command's error message points operators at it.
AUTHORIZATION_DOMAIN = "aipotluck.spend-authorization.v1"

#: Where ``join``/``ask``/``doctor``/``share`` keep a joiner's identity,
#: its in-flight request, and a contribution node's configuration.
DEFAULT_CONFIG_DIR = "~/.aipotluck"
_CONFIG_FILE = "config.json"
_KEY_FILE = "key.json"
_PENDING_FILE = "pending.json"
_NODE_FILE = "node.json"
_LAST_ERROR_FILE = "last_error.json"
#: Where ``join --node`` keeps the platform node-link token the server returned
#: once. A file of its own rather than a field of ``key.json`` or
#: ``config.json``: ``config.json`` holds only non-secret identifiers (it is
#: written world-readable and ``doctor`` reasons about it), ``key.json`` is the
#: identity's key material, and a platform credential has a different lifecycle
#: from both — it is issued by the server, it can be revoked on its own, and
#: losing it is repaired by re-joining, not by regenerating a key. Like
#: ``key.json`` it is written 0600 by :func:`_write_secret_file` and its value
#: is never printed.
_NODE_TOKEN_FILE = "node_token.json"

#: Defaults for ``ask`` when the flags are omitted.
#
#: There is deliberately no default *model*. A hardcoded one (this build used to
#: default to the test pool's name) is a guess the consumer cannot verify and
#: the server will not honour: model names are whatever each contributor's
#: upstream calls them. ``ask`` therefore reads the models the online nodes
#: actually advertise through ``GET /v1/nodes`` and picks the one most of them
#: serve, and says so when no node is online rather than dispatching a request
#: that provably cannot be routed.
ASK_DEFAULT_MAX_CHARGE = 20_000
#: Agent sessions are a first-class workload: thirty minutes covers a long
#: buffered turn without anyone tuning a flag, `--timeout` lowers or raises it,
#: and streaming calls are not length-bound by it anyway — increments keep the
#: connection alive however long the answer runs.
ASK_DEFAULT_TIMEOUT_S = 1800.0

#: How long ``ask`` tolerates a request that has no node to run it on before it
#: gives up. This is what keeps "no nodes" from becoming an unbounded wait: the
#: server leaves such a request in ``ROUTING`` forever, so the bound has to be
#: imposed by the client. The value is short because "nothing happened for a
#: while and there is nobody to do the work" is the useful signal.
ASK_NO_NODE_GRACE_S = 3.0

#: How often the streaming consumer checks its deadline, the cancellation flag,
#: and whether a node exists. It is a poll interval, not a network setting: the
#: socket is read on a separate thread.
ASK_STREAM_POLL_S = 0.2

#: Pause between a dropped stream and reconnecting from the last cursor.
ASK_RECONNECT_PAUSE_S = 0.3

#: How long a user-initiated cancel may take to reach a terminal state before
#: ``ask`` stops waiting and simply reports the cancellation.
ASK_CANCEL_GRACE_S = 10.0

#: Conservative defaults a contributor wizard writes. Why 1 and 20_000:
#: ``max_concurrency=1`` means one shared call at a time, so a new contributor
#: cannot accidentally melt their own upstream quota on day one;
#: ``max_local_reserved_milli_credits=20_000`` caps how much *exposure* the node
#: will carry before checking in with the operator — it is the ceiling on the
#: sum of the maximum charges of in-flight attempts the node admits, not a
#: spending budget.
#:
#: The value is ``ASK_DEFAULT_MAX_CHARGE`` and ``ConnectorConfig``'s own default
#: on purpose, and all three must move together. It used to be 5_000 here while
#: the other two were 20_000, which made the very first request of an
#: all-default deployment fail in the node's local policy ("local budget would
#: be exceeded: 0 + 20000 > 5000") — a wizard-configured node was more
#: restrictive than a programmatic one, and a default consumer request could
#: never fit it. A node that wants a smaller ceiling sets one explicitly.
NODE_DEFAULT_MAX_CONCURRENCY = 1
NODE_DEFAULT_MAX_LOCAL_RESERVED_MILLI_CREDITS = 20_000

#: Environment variables the contributor wizard reads secrets from, so a key
#: never has to appear in a command line (where every process on the host can
#: read it out of ``ps``) or in the shell's history.
UPSTREAM_KEY_ENV = "AIPOTLUCK_UPSTREAM_KEY"
NODE_TOKEN_ENV = "AIPOTLUCK_NODE_TOKEN"

#: Exit status for a run the user cancelled with Ctrl-C. 130 is the
#: conventional "terminated by SIGINT" code, and it keeps a user cancel
#: distinguishable from an ordinary failure (1) in scripts.
EXIT_CANCELLED = 130


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with one subparser per command.

    Returns:
        The fully configured parser. Each subparser records its handler via
        ``set_defaults`` so dispatch needs no separate table.
    """
    # Function-local, as every import of :mod:`aipotluck.localapi` in this module
    # is: that module drives the consumer seams defined here, so the dependency
    # runs one way and a module-level import would be a cycle. Its constants are
    # read here rather than restated, so a default can be written down once.
    from . import localapi

    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Offline tooling for the AI share platform: generate an Ed25519 key "
            "pair, sign a JSON payload under a domain, and verify the result."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser(
        "keygen",
        help="Generate an Ed25519 key pair.",
        description=(
            "Generate an Ed25519 key pair. The public key is printed to stdout; "
            "the private key is written to --out with mode 0600."
        ),
    )
    keygen.add_argument(
        "--out",
        required=True,
        type=Path,
        metavar="PATH",
        help="File to write the base64 private key to. Refuses to overwrite.",
    )
    keygen.add_argument(
        "--force",
        action="store_true",
        help="Overwrite --out if it already exists.",
    )
    keygen.set_defaults(handler=_cmd_keygen)

    sign = subparsers.add_parser(
        "sign",
        help="Sign a JSON payload under a domain string.",
        description=(
            "Sign a JSON payload under a domain string. The payload is parsed "
            "with the strict canonical rules, so floats and duplicate keys are "
            "rejected exactly as they would be during verification."
        ),
    )
    sign.add_argument(
        "--key",
        required=True,
        type=Path,
        metavar="PATH",
        help="Private key file produced by `keygen`.",
    )
    sign.add_argument(
        "--domain",
        required=True,
        help="Domain separation string, e.g. AIPotluck/settlement/v1.",
    )
    sign.add_argument(
        "--payload",
        required=True,
        type=Path,
        metavar="PATH",
        help="JSON file to sign.",
    )
    sign.set_defaults(handler=_cmd_sign)

    verify = subparsers.add_parser(
        "verify",
        help="Verify one signature, or one receipt against the server's books.",
        description=(
            "Two unrelated checks share this command. With no subcommand it "
            "verifies a domain-separated signature and exits 0 with OK when it "
            "matches, otherwise exits 1 with the reason. With the `receipt` "
            "subcommand it runs the five-dimension offline verification over one "
            "receipt and reports each dimension separately."
        ),
    )
    verify.add_argument(
        "--public-key",
        dest="public_key",
        default=None,
        help="Base64 Ed25519 public key printed by `keygen`.",
    )
    verify.add_argument(
        "--domain",
        default=None,
        help="Domain separation string used when signing.",
    )
    verify.add_argument(
        "--payload",
        type=Path,
        default=None,
        metavar="PATH",
        help="JSON file that was signed.",
    )
    verify.add_argument(
        "--signature",
        default=None,
        help="Base64 signature produced by `sign`.",
    )
    verify_sub = verify.add_subparsers(dest="verify_command")

    verify_receipt = verify_sub.add_parser(
        "receipt",
        help="Run the five-dimension offline verification over one receipt.",
        description=(
            "Recompute one receipt against the records that back it and report "
            "five dimensions separately: signature_valid, metering_provenance, "
            "allocation_valid, ledger_included and account_history_complete. "
            "They fail independently, and a single green light would otherwise "
            "read as proof the upstream really consumed the tokens.\n\n"
            "The receipt, the checkpoint and this account's history are fetched "
            "over HTTP under the identity `join` saved; metering_provenance "
            "then stays `unknown`, because a consumer holds no signed usage "
            "report, and the command says so rather than substituting the "
            "receipt's declared level. The operator-side view that reads a "
            "server's own database is `potluck-server verify receipt`.\n\n"
            "Nothing is written and nothing is frozen. Exit 0 when no dimension "
            "reported a contradiction, 1 when one did; `unknown` is not a "
            "contradiction."
        ),
    )
    _add_config_dir_argument(verify_receipt)
    verify_receipt.add_argument(
        "--receipt-id",
        required=True,
        dest="receipt_id",
        metavar="ID",
        help="The receipt to verify, e.g. R-1 as printed by `ask`.",
    )
    verify_receipt.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print the report as one canonical JSON document.",
    )
    verify_receipt.set_defaults(handler=_cmd_verify_receipt)
    verify.set_defaults(handler=_cmd_verify)
    connect = subparsers.add_parser(
        "connect",
        help="Run a contribution node from its node.json until SIGINT/SIGTERM.",
        description=(
            "Load a node configuration, probe the configured upstream, and "
            "hold the outbound link to the server until interrupted."
        ),
    )
    connect.add_argument(
        "--config",
        required=True,
        type=Path,
        metavar="PATH",
        help="node.json produced for this node.",
    )
    connect.set_defaults(handler=_cmd_connect)

    request = subparsers.add_parser(
        "request",
        help="Submit a consumer request carrying a signed authorization.",
        description=(
            "POST /v1/requests with the signed spend authorization from "
            "--auth-file. With --wait, poll until the request settles and "
            "print the status document and receipt summary."
        ),
    )
    request.add_argument("--config", required=True, type=Path, metavar="PATH",
                         help="consumer.json with server_url and token.")
    request.add_argument("--prompt", required=True, help="The request text.")
    request.add_argument("--model", required=True, help="Model being requested.")
    request.add_argument("--request-id", required=True, dest="request_id",
                         help="Request ID; must match the signed authorization.")
    request.add_argument("--idempotency-key", required=True, dest="idempotency_key",
                         help="Client idempotency key, scoped to the tenant.")
    request.add_argument("--max-charge", required=True, type=int, dest="max_charge",
                         metavar="MILLI_CREDITS",
                         help="Charge ceiling; may not exceed the signed cap.")
    request.add_argument("--auth-file", type=Path, default=None, dest="auth_file",
                         metavar="PATH",
                         help="JSON file with the serialized signed authorization.")
    request.add_argument("--wait", action="store_true",
                         help="Poll until settlement or a terminal status.")
    request.set_defaults(handler=_cmd_request)

    status = subparsers.add_parser(
        "status",
        help="Print a request's status document as canonical JSON.",
        description="GET /v1/requests/{id} and print the document canonically.",
    )
    status.add_argument("--config", required=True, type=Path, metavar="PATH",
                        help="consumer.json with server_url and token.")
    status.add_argument("--request-id", required=True, dest="request_id")
    status.set_defaults(handler=_cmd_status)

    events = subparsers.add_parser(
        "events",
        help="Print a request's stored events from the gateway's SSE stream.",
        description=(
            "Read GET /v1/requests/{id}/events from --cursor, print every "
            "replayed event, and stop at the terminal done frame."
        ),
    )
    events.add_argument("--config", required=True, type=Path, metavar="PATH",
                        help="consumer.json with server_url and token.")
    events.add_argument("--request-id", required=True, dest="request_id")
    events.add_argument("--cursor", type=int, default=0,
                        help="Highest event cursor already seen (default 0).")
    events.set_defaults(handler=_cmd_events)

    version = subparsers.add_parser(
        "version",
        help="Print the package and protocol versions.",
        description="Print `potluck <version> (protocol <schema version>)`.",
    )
    version.set_defaults(handler=_cmd_version)

    join = subparsers.add_parser(
        "join",
        help="Redeem an invite and save this machine's identity.",
        description=(
            "Generate an Ed25519 key (mode 0600), redeem the invite code at the "
            "server, and write the identity to the config directory. The private "
            "key is never printed. With --node the server also issues this "
            "node's platform link token, which is saved to node_token.json "
            "(0600) and never printed; the contributor wizard then needs no "
            "token from you. An invite the operator minted --with-api-key also "
            "comes back with an API key for the server's OpenAI-compatible "
            "endpoint, printed once on its own line and not saved here. "
            "Re-running with the saved key is idempotent and issues no second "
            "token and no second key."
        ),
    )
    join.add_argument("--server", required=True, help="Server base URL, e.g. http://host:port.")
    join.add_argument(
        "--invite", required=True, help="Invite code from `potluck-server admin invite`."
    )
    join.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        dest="config_dir",
        metavar="DIR",
        help=f"Where to store the identity (default {DEFAULT_CONFIG_DIR}).",
    )
    join.add_argument(
        "--subject-id",
        default=None,
        dest="subject_id",
        help=(
            "Deprecated and display-only: the identity is derived by the server "
            "and a client-chosen subject has no effect. Used as a display label "
            "when --display-name is absent."
        ),
    )
    join.add_argument(
        "--display-name", default=None, dest="display_name", help="Optional label."
    )
    join.add_argument(
        "--node",
        action="store_true",
        help=(
            "Join as a contribution node. The device identifiers are derived by "
            "the server from this join's key; only the device public key is sent."
        ),
    )
    join.add_argument(
        "--no-setup",
        action="store_true",
        dest="no_setup",
        help=(
            "For --node joins, stop after saving the identity instead of also "
            "running the contributor wizard (upstream configuration + self-check)."
        ),
    )
    _add_share_wizard_arguments(join)
    join.set_defaults(handler=_cmd_join)

    ask = subparsers.add_parser(
        "ask",
        help="Ask the server a question with the joined identity.",
        description=(
            "Sign a request with the identity saved by `join`, dispatch it, wait "
            "for the answer, and print the text and a cost line. With --stream "
            "the answer is requested in stream_strict mode and each increment is "
            "printed as it arrives. `--json` prints one machine-readable document."
        ),
    )
    ask.add_argument("question", help="The question to ask.")
    ask.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        dest="config_dir",
        metavar="DIR",
        help=f"Where the identity lives (default {DEFAULT_CONFIG_DIR}).",
    )
    ask.add_argument(
        "--model",
        default=None,
        help=(
            "Model to request. By default the model the online contribution "
            "nodes advertise (the commonest one, ties broken alphabetically), "
            "read from GET /v1/nodes."
        ),
    )
    ask.add_argument(
        "--max-charge",
        type=int,
        default=ASK_DEFAULT_MAX_CHARGE,
        dest="max_charge",
        metavar="MILLI_CREDITS",
        help="Charge ceiling (default 20000).",
    )
    ask.add_argument(
        "--timeout",
        type=float,
        default=ASK_DEFAULT_TIMEOUT_S,
        help="Seconds to wait for a terminal status (default 120).",
    )
    ask.add_argument(
        "--stream",
        action="store_true",
        help=(
            "Request stream_strict delivery and print each increment as it "
            "arrives. Without it the request is buffered and the answer is "
            "printed once, after a clear 'waiting' notice."
        ),
    )
    ask.add_argument(
        "--no-node-grace",
        type=float,
        default=ASK_NO_NODE_GRACE_S,
        dest="no_node_grace",
        metavar="SECONDS",
        help=(
            "How long to wait for a node to appear before reporting 'no node' "
            f"(default {ASK_NO_NODE_GRACE_S:g}). This bounds the wait instead of "
            "letting it run to --timeout."
        ),
    )
    ask.add_argument(
        "--resume",
        action="store_true",
        help="Resume the pending request instead of starting one.",
    )
    ask.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print a machine-readable document.",
    )
    ask.set_defaults(handler=_cmd_ask)

    serve_openai = subparsers.add_parser(
        "serve-openai",
        help="Serve this machine's identity to local tools as an OpenAI/Anthropic endpoint.",
        description=(
            "Run a loopback HTTP endpoint that accepts the request shapes tools "
            "already speak — OpenAI's POST /v1/chat/completions and GET "
            "/v1/models, and Anthropic's POST /v1/messages, which is what Claude "
            "Code speaks — and turns each one into a request on this machine's own "
            "signed consumer path, exactly as `ask` does. Buffered and streaming "
            "answers are both served, and a request that does not reach SUCCEEDED "
            "is answered as an error naming the platform's request id and the way "
            "to check what it cost.\n\n"
            "The endpoint binds an IPv4 loopback address and refuses to start on "
            "anything else, and it checks no API key: any process able to open a "
            "loopback port could equally run `ask` against the same key file, so "
            "loopback reachability *is* the threat model rather than a key check "
            "that would add a prompt and no boundary. What loopback binding does "
            "buy is the property that matters: the signing key never leaves this "
            "machine, which is the difference from the server's own "
            "OpenAI-compatible surface at <server-url>/openai/v1, where the server "
            "holds bounded spending authority over a key it issued.\n\n"
            f"Point a tool at it with `{PROG} config emit --shell` or "
            f"`{PROG} config emit --claude-code`."
        ),
    )
    _add_config_dir_argument(serve_openai)
    serve_openai.add_argument(
        "--host",
        default=localapi.LOOPBACK_HOST,
        help=(
            "Address to bind. An IPv4 loopback address only; 0.0.0.0, ::, a "
            "public address and a hostname are all refused, because an endpoint "
            "that spends this identity must not be reachable from another "
            f"machine (default {localapi.LOOPBACK_HOST})."
        ),
    )
    serve_openai.add_argument(
        "--port",
        type=int,
        default=localapi.DEFAULT_PORT,
        help=f"Port to listen on (default {localapi.DEFAULT_PORT}).",
    )
    serve_openai.add_argument(
        "--max-charge",
        type=int,
        default=ASK_DEFAULT_MAX_CHARGE,
        dest="max_charge",
        metavar="MILLI_CREDITS",
        help=(
            "Charge ceiling signed into every authorization this endpoint mints "
            f"(default {ASK_DEFAULT_MAX_CHARGE})."
        ),
    )
    serve_openai.add_argument(
        "--timeout",
        type=float,
        default=ASK_DEFAULT_TIMEOUT_S,
        help=(
            "Seconds one request waits for a terminal status "
            f"(default {ASK_DEFAULT_TIMEOUT_S:g})."
        ),
    )
    serve_openai.add_argument(
        "--no-node-grace",
        type=float,
        default=ASK_NO_NODE_GRACE_S,
        dest="no_node_grace",
        metavar="SECONDS",
        help=(
            "How long an empty node list is tolerated before a request is "
            f"reported as unroutable (default {ASK_NO_NODE_GRACE_S:g})."
        ),
    )
    serve_openai.set_defaults(handler=_cmd_serve_openai)

    doctor = subparsers.add_parser(
        "doctor",
        help="Export a redacted diagnostic report for this machine.",
        description=(
            "Print version, configuration summary, server reachability, whether "
            "the saved credential still works, contribution-node status, the "
            "count of unfinished local requests, and the last error code. The "
            "report never contains a token, a private key, or an upstream key."
        ),
    )
    doctor.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        dest="config_dir",
        metavar="DIR",
        help=f"Where the identity lives (default {DEFAULT_CONFIG_DIR}).",
    )
    doctor.add_argument(
        "--node-config",
        type=Path,
        default=None,
        dest="node_config",
        metavar="PATH",
        help="A node.json to include in the report (default <config-dir>/node.json).",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print the report as one canonical JSON document.",
    )
    doctor.set_defaults(handler=_cmd_doctor)

    share = subparsers.add_parser(
        "share",
        help="Configure, run or pause this machine's contribution node.",
        description=(
            "The contributor side of the platform. `setup` walks through the "
            "upstream configuration and the self-check; `run` starts sharing; "
            "`pause`/`resume` flip the local share switch without editing JSON "
            "by hand; `status` prints the local picture."
        ),
    )
    share_sub = share.add_subparsers(dest="share_command", required=True)

    share_setup = share_sub.add_parser(
        "setup",
        help="Collect the upstream settings, write node.json, and self-check.",
        description=(
            "Confirm this machine's upstream address/key/model, write them into "
            "the local node configuration, then check upstream reachability, "
            "configuration completeness, and server reachability. The platform "
            "node token is normally the one `join --node` already saved, so an "
            "invited contributor is not asked for it; --node-token-file or "
            f"{NODE_TOKEN_ENV} override it for a token an operator provisioned "
            "in the server by hand. The upstream key and any token are read "
            "from a file or the environment, never from a command-line "
            "argument, and are never printed."
        ),
    )
    _add_share_wizard_arguments(share_setup)
    _add_config_dir_argument(share_setup)
    share_setup.set_defaults(handler=_cmd_share_setup)

    share_run = share_sub.add_parser(
        "run",
        help="Start sharing this machine's upstream until interrupted.",
        description=(
            "Load node.json, check the configured upstream, and hold the "
            "outbound link to the server. `share pause`/`resume` from another "
            "shell take effect within the poll interval without a restart."
        ),
    )
    share_run.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="node.json (default <config-dir>/node.json).",
    )
    _add_config_dir_argument(share_run)
    share_run.set_defaults(handler=_cmd_share_run)

    share_pause = share_sub.add_parser(
        "pause",
        help="Stop accepting new work without stopping the node.",
        description=(
            "Set share_enabled=false in the local node configuration. A running "
            "`potluck share run` picks it up within its poll interval and the "
            "server stops offering it new attempts; work already in flight is "
            "allowed to finish."
        ),
    )
    share_pause.add_argument("--config", type=Path, default=None, metavar="PATH",
                             help="node.json (default <config-dir>/node.json).")
    _add_config_dir_argument(share_pause)
    share_pause.set_defaults(handler=_cmd_share_pause)

    share_resume = share_sub.add_parser(
        "resume",
        help="Accept new work again after a pause.",
        description=(
            "Set share_enabled=true in the local node configuration. A running "
            "`potluck share run` resumes accepting offers within its poll "
            "interval."
        ),
    )
    share_resume.add_argument("--config", type=Path, default=None, metavar="PATH",
                              help="node.json (default <config-dir>/node.json).")
    _add_config_dir_argument(share_resume)
    share_resume.set_defaults(handler=_cmd_share_resume)

    share_status = share_sub.add_parser(
        "status",
        help="Print this machine's share state, redacted.",
        description=(
            "Show whether sharing is enabled, the local limits, the configured "
            "upstream, and whether the server currently lists this node. Never "
            "prints a token, a private key, or an upstream key."
        ),
    )
    share_status.add_argument("--config", type=Path, default=None, metavar="PATH",
                              help="node.json (default <config-dir>/node.json).")
    _add_config_dir_argument(share_status)
    share_status.add_argument("--json", action="store_true", dest="json_output",
                              help="Print the state as one canonical JSON document.")
    share_status.set_defaults(handler=_cmd_share_status)

    share_reconcile = share_sub.add_parser(
        "reconcile",
        help="Diff this node's own record against what the server claims.",
        description=(
            "Compare what this machine durably recorded — every execution it "
            "started, how far the server acknowledged it, and any usage report "
            "it signed that is still undelivered — with the attempts the server "
            "says it dispatched to this node. Both directions are checked: an "
            "execution the server never heard of is work this node may have paid "
            "an upstream for and never been billed for, and a server attempt "
            "with no local execution is a dispatch this node has no record of "
            "running.\n\n"
            "Reads node.db and one read-only server endpoint scoped to this "
            "node's own credential. Nothing is written on either side and no "
            "account is frozen. Exit 0 when the two sides agree, 1 when any "
            "difference stands, and every difference names its attempt."
        ),
    )
    share_reconcile.add_argument("--config", type=Path, default=None, metavar="PATH",
                                 help="node.json (default <config-dir>/node.json).")
    _add_config_dir_argument(share_reconcile)
    share_reconcile.add_argument("--json", action="store_true", dest="json_output",
                                 help="Print the differences as one canonical JSON document.")
    share_reconcile.set_defaults(handler=_cmd_share_reconcile)

    contribute = subparsers.add_parser(
        "contribute",
        help="Configure what this machine shares with the pool.",
        description=(
            "The contributor's configuration side of the platform, for the steps "
            "that are not the interactive wizard. `import-config` reads the "
            "upstream a tool on this machine is already configured with and "
            "writes it into node.json, so a credential is not retyped into a "
            "second place where it can go stale."
        ),
    )
    contribute_sub = contribute.add_subparsers(
        dest="contribute_command", required=True
    )

    import_config = contribute_sub.add_parser(
        "import-config",
        help="Write the upstream a tool on this machine already uses into node.json.",
        description=(
            "Read an upstream endpoint, model and API key out of a configuration "
            "that already exists on this machine, and write them into the local "
            "node configuration (mode 0600). One source is named per run:\n\n"
            "  --from-claude-code  Claude Code's settings.json, whose env block "
            "names ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY, "
            "and ANTHROPIC_MODEL. The upstream is read as Anthropic-shaped.\n"
            "  --from-codex        Codex's config.toml, whose model, "
            "model_provider and model_providers.<name>.base_url/env_key name the "
            "same three things. The credential is read from the environment "
            "variable env_key names, which is where Codex reads it.\n"
            "  --from-env          The OPENAI_* variables, or the ANTHROPIC_* "
            "ones; the family that is set decides the wire style.\n\n"
            "A tool names a base URL and this node's adapter POSTs to exactly the "
            "URL it is given, so the completion path is completed here. A field "
            "the source does not name is reported by the key path it was looked "
            "for, together with every other missing field of that source and the "
            "argument that would supply it; --upstream-endpoint, "
            "--upstream-model and --upstream-key-file fill a gap without editing "
            "another program's file. An incomplete import writes nothing.\n\n"
            "An existing node.json keeps its token, device binding, limits and "
            "share switch — only its adapter block is replaced. With no node.json "
            "yet, one is created from the identity `join --node` saved, which "
            "needs the platform node token the invite path already stored. The "
            "upstream key is never printed and never leaves this machine."
        ),
    )
    source = import_config.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-claude-code",
        nargs="?",
        const=str(configimport.CLAUDE_CODE_SETTINGS),
        default=None,
        dest="from_claude_code",
        metavar="PATH",
        help=(
            "Import from Claude Code's settings.json (default "
            f"{configimport.CLAUDE_CODE_SETTINGS})."
        ),
    )
    source.add_argument(
        "--from-codex",
        nargs="?",
        const=str(configimport.CODEX_CONFIG),
        default=None,
        dest="from_codex",
        metavar="PATH",
        help=(
            f"Import from Codex's config.toml (default "
            f"{configimport.CODEX_CONFIG})."
        ),
    )
    source.add_argument(
        "--from-env",
        action="store_true",
        dest="from_env",
        help=(
            "Import from the environment: OPENAI_BASE_URL/OPENAI_API_KEY/"
            "OPENAI_MODEL, or ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/"
            "ANTHROPIC_MODEL."
        ),
    )
    _add_upstream_arguments(import_config, style_default=None)
    _add_node_token_argument(import_config)
    _add_config_dir_argument(import_config)
    import_config.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="node.json to write (default <config-dir>/node.json).",
    )
    import_config.set_defaults(handler=_cmd_contribute_import_config)

    config = subparsers.add_parser(
        "config",
        help="Emit the configuration the tools on this machine need.",
        description=(
            "The consumer's configuration side of the platform: `emit` prints the "
            "block that points a tool at the loopback endpoint "
            "`potluck serve-openai` runs, so the base URL is not assembled by "
            "hand. Nothing is written and no credential is read."
        ),
    )
    config_sub = config.add_subparsers(dest="config_command", required=True)

    emit = config_sub.add_parser(
        "emit",
        help="Print the configuration that points a tool at the local endpoint.",
        description=(
            "Print a ready-to-paste configuration block for the loopback endpoint, "
            "on stdout, with the guidance on stderr so the block can be piped into "
            "a merge without the guidance landing inside a settings file.\n\n"
            "  --shell        `export` lines for an OpenAI-compatible tool: "
            "OPENAI_BASE_URL carries /v1, because such a client appends "
            "chat/completions to whatever it is given.\n"
            "  --claude-code  the env block of Claude Code's settings.json: "
            "ANTHROPIC_BASE_URL is the endpoint's root, because Claude Code "
            "appends /v1/messages itself, which the endpoint serves.\n\n"
            "The api_key both emit is a documented placeholder, not a credential: "
            "the endpoint checks no key and signs each request with this machine's "
            "joined identity instead. Pass --model to name the model a tool should "
            "ask for; without it a tool sends its own default name, which the pool "
            "may not serve, and the guidance says so."
        ),
    )
    shape = emit.add_mutually_exclusive_group(required=True)
    shape.add_argument(
        "--shell",
        action="store_true",
        help="Print `export` lines for an OpenAI-compatible tool.",
    )
    shape.add_argument(
        "--claude-code",
        action="store_true",
        dest="claude_code",
        help="Print the env block of Claude Code's settings.json as one JSON document.",
    )
    emit.add_argument(
        "--host",
        default=localapi.LOOPBACK_HOST,
        help=(
            "The endpoint's address, which must be the IPv4 loopback address it "
            f"binds (default {localapi.LOOPBACK_HOST})."
        ),
    )
    emit.add_argument(
        "--port",
        type=int,
        default=localapi.DEFAULT_PORT,
        help=f"The endpoint's port (default {localapi.DEFAULT_PORT}).",
    )
    emit.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help=(
            "Model a tool should ask for; it must be one the pool's nodes "
            "advertise, which `curl -s <base_url>/models` lists."
        ),
    )
    emit.set_defaults(handler=_cmd_config_emit)

    return parser


def _add_config_dir_argument(parser: argparse.ArgumentParser) -> None:
    """Add the shared ``--config-dir`` argument the local-state commands need."""
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        dest="config_dir",
        metavar="DIR",
        help=f"Where local state lives (default {DEFAULT_CONFIG_DIR}).",
    )


def _add_upstream_arguments(
    parser: argparse.ArgumentParser, *, style_default: str | None
) -> None:
    """Add the arguments that name an upstream.

    The upstream API key is deliberately *not* accepted as a value: a secret on a
    command line is visible to every process on the host through ``ps`` and is
    kept in the shell's history, so it is read from a file path or an environment
    variable instead, or prompted for on a terminal.

    Args:
        parser: The subparser to add to.
        style_default: The default wire style, or `None` to omit ``--upstream-style``
            entirely. Omitted for a command whose source decides the style: an
            imported Claude Code configuration is Anthropic-shaped and an imported
            Codex one is OpenAI-shaped, and an override there would let an operator
            configure a node that posts one provider's body at the other's
            endpoint — a failure that surfaces at the upstream, not here.
    """
    parser.add_argument(
        "--upstream-endpoint",
        default=None,
        dest="upstream_endpoint",
        metavar="URL",
        help=(
            "Full upstream completion URL, e.g. "
            "https://api.example/v1/chat/completions. The adapter POSTs to "
            "exactly this URL, so a base URL without the path will fail."
        ),
    )
    parser.add_argument(
        "--upstream-model",
        default=None,
        dest="upstream_model",
        metavar="MODEL",
        help="Model this node executes (e.g. gpt-4o-mini).",
    )
    if style_default is not None:
        parser.add_argument(
            "--upstream-style",
            default=style_default,
            choices=["openai", "anthropic"],
            dest="upstream_style",
            help=f"Wire style of the upstream (default {style_default}).",
        )
    parser.add_argument(
        "--upstream-name",
        default="main",
        dest="upstream_name",
        help="Label for this upstream in diagnostics (default main).",
    )
    parser.add_argument(
        "--upstream-key-file",
        type=Path,
        default=None,
        dest="upstream_key_file",
        metavar="PATH",
        help=(
            f"File holding the upstream API key (whitespace-trimmed). "
            f"Alternatively set {UPSTREAM_KEY_ENV}."
        ),
    )


def _add_node_token_argument(parser: argparse.ArgumentParser) -> None:
    """Add the platform node-token override the node-writing commands accept.

    A file path or an environment variable rather than a value, for the same
    reason as the upstream key: a token on a command line is readable by every
    process on the host and is kept in the shell's history.
    """
    parser.add_argument(
        "--node-token-file",
        type=Path,
        default=None,
        dest="node_token_file",
        metavar="PATH",
        help=(
            "File holding a platform node-link token the operator provisioned "
            "in the server's credentials[] by hand, overriding the one `join` "
            f"saved. Alternatively set {NODE_TOKEN_ENV}. The invite path needs "
            "neither."
        ),
    )


def _add_share_wizard_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the contributor wizard's non-secret inputs.

    The two secrets — the upstream API key and the platform node token — are
    deliberately *not* accepted as their own flags. A value passed on a command
    line is visible to every process on the host through ``ps`` and is kept in
    the shell's history, so both are read from a file path or an environment
    variable instead, or prompted for on a terminal.
    """
    _add_upstream_arguments(parser, style_default="openai")
    _add_node_token_argument(parser)
    parser.add_argument(
        "--probe-upstream-live",
        action="store_true",
        dest="probe_upstream_live",
        help=(
            "During the self-check, send a real (billable) request to the "
            "upstream to prove the credential works. Off by default: the check "
            " otherwise only proves the host is reachable."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the live upstream probe without an interactive prompt.",
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="Start sharing immediately after the self-check passes.",
    )


def read_text_file(path: Path, *, what: str) -> str:
    """Read a UTF-8 text file, failing with an actionable message when absent.

    Args:
        path: The file to read.
        what: Human-readable description of the file's role, used in the error.

    Returns:
        The file contents.

    Raises:
        PotluckError: If the file cannot be read.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PotluckError(f"{what} not found: {path}") from exc
    except IsADirectoryError as exc:
        raise PotluckError(f"{what} is a directory, not a file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise PotluckError(f"{what} is not valid UTF-8 text: {path}") from exc


def _load_payload(path: Path) -> dict[str, Any]:
    """Parse a JSON payload file under the strict canonical rules.

    Args:
        path: The JSON file to parse.

    Returns:
        The parsed JSON object.

    Raises:
        PotluckError: If the file cannot be read.
        CanonicalizationError: If the JSON is invalid, repeats a key, uses a
            float, contains an integer past the safe range, or is not a JSON
            object at the top level.
    """
    raw = read_text_file(path, what="payload file")
    value = canonical.loads_strict(raw)
    if not isinstance(value, dict):
        raise CanonicalizationError(
            f"payload file {path} must contain a JSON object at the top level, "
            f"got {type(value).__name__}"
        )
    return value


def _load_public_key(public_key_b64: str) -> Any:
    """Load an Ed25519 public key from its base64 text form.

    Args:
        public_key_b64: Base64-encoded 32-byte Ed25519 public key.

    Returns:
        An ``Ed25519PublicKey`` instance.

    Raises:
        SignatureError: If the text is not valid base64.
        UnknownKeyError: If the decoded bytes are not a 32-byte Ed25519 key.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw = canonical.base64_decode(public_key_b64.strip())
    if len(raw) != 32:
        raise UnknownKeyError(f"Ed25519 public keys are 32 bytes, got {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


def _cmd_keygen(args: argparse.Namespace) -> int:
    """Run the ``keygen`` subcommand.

    The private key is created with ``O_EXCL`` when ``--force`` is absent so a
    concurrent creation cannot be clobbered between the existence check and the
    open. The descriptor is then forced to mode 0600 with ``os.fchmod`` *before*
    any byte is written, which is what keeps the key unreadable to group and
    other even when ``--force`` truncates a pre-existing file: the kernel
    ignores ``os.open``'s mode argument for an existing inode, so a target left
    at 0644 would otherwise receive the fresh secret while still world-readable.
    Setting the mode on the descriptor also makes the result independent of the
    process umask, which the create-time mode argument is subject to.

    Args:
        args: Parsed arguments carrying ``out`` and ``force``.

    Returns:
        0 on success, 1 when the target exists and ``--force`` was not given.
    """
    out: Path = args.out
    if out.exists() and not args.force:
        print(
            f"{PROG}: error: refusing to overwrite existing key file {out}; "
            "pass --force to replace it",
            file=sys.stderr,
        )
        return 1

    public_key_b64, private_key_b64 = identity.generate_key_pair()
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if args.force else os.O_EXCL)
    fd = os.open(out, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (private_key_b64 + "\n").encode("utf-8"))
    finally:
        os.close(fd)

    print(public_key_b64)
    print(f"{PROG}: private key written to {out} (mode 0600)", file=sys.stderr)
    return 0


def _cmd_sign(args: argparse.Namespace) -> int:
    """Run the ``sign`` subcommand.

    Args:
        args: Parsed arguments carrying ``key``, ``domain`` and ``payload``.

    Returns:
        0 on success, with the base64 signature printed to stdout.
    """
    private_key_b64 = read_text_file(args.key, what="private key file").strip()
    private_key = identity.load_private_key(private_key_b64)
    payload = _load_payload(args.payload)
    signature = canonical.sign_payload(args.domain, payload, private_key)
    print(signature)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """Run the ``verify`` subcommand: one signature against one payload.

    The four flags are optional at the parser level because ``verify receipt``
    shares this command and takes none of them, so they are checked here instead.
    A missing one is a usage error and keeps argparse's status 2.

    Args:
        args: Parsed arguments carrying ``public_key``, ``domain``, ``payload``
            and ``signature``.

    Returns:
        0 when the signature verifies, 1 when it does not, 2 when a flag is
        missing.
    """
    missing = [
        flag
        for flag, value in (
            ("--public-key", args.public_key),
            ("--domain", args.domain),
            ("--payload", args.payload),
            ("--signature", args.signature),
        )
        if value is None
    ]
    if missing:
        print(
            f"{PROG}: error: a signature check needs {', '.join(missing)}; "
            f"or run `{PROG} verify receipt` to verify a receipt instead",
            file=sys.stderr,
        )
        return 2
    public_key = _load_public_key(args.public_key)
    payload = _load_payload(args.payload)
    if canonical.verify_payload(args.domain, payload, args.signature, public_key):
        print(f"OK: signature is valid for this payload under domain {args.domain!r}")
        return 0
    print(
        f"{PROG}: FAIL: signature does not match the payload under domain "
        f"{args.domain!r}",
        file=sys.stderr,
    )
    return 1


def install_stop_handlers(stop: threading.Event) -> bool:
    """Set `stop` on SIGINT/SIGTERM.

    Returns:
        False when the caller is not the main thread, where the OS forbids
        installing handlers; the caller then simply waits on the event, which
        is what keeps a library caller of ``main`` from dying on signal.setup.
    """
    def _handle(signum: int, _frame: Any) -> None:
        del signum  # the event is the same for every signal
        stop.set()

    try:
        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
    except ValueError:
        return False
    return True


def _cmd_connect(args: argparse.Namespace) -> int:
    """Run the ``connect`` subcommand: a node until SIGINT/SIGTERM.

    Before dialing the server, an http adapter's upstream is probed without
    credentials, so an operator learns about blocked egress before the server
    dispatches work to this node and every attempt fails at the adapter. This is
    the same routine ``share run`` uses, so the two entry points cannot drift.
    """
    return _run_node_forever(args.config)


def _load_authorization(path: Path) -> dict[str, Any]:
    """Read the serialized signed authorization a client would POST.

    Raises:
        PotluckError: If the file cannot be read, is not strict JSON, or is
            not a JSON object.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PotluckError(f"authorization file could not be read: {path}: {exc}") from exc
    value = canonical.loads_strict(text)
    if not isinstance(value, dict):
        raise CanonicalizationError(
            f"authorization file {path} must contain a JSON object at the top level"
        )
    return value


def _cmd_request(args: argparse.Namespace) -> int:
    """Run the ``request`` subcommand: POST /v1/requests, optionally waiting.

    Without ``--auth-file`` the command fails before any network traffic: the
    gateway admits only signed spend authorizations, and signing is done by
    ``potluck sign`` with the consumer key. With ``--wait`` the status document
    is printed once the request settles or turns terminal, followed by the
    receipt summary when one exists; a terminal non-success status exits 1.
    """
    if args.auth_file is None:
        print(
            f"{PROG}: error: --auth-file is required: POST /v1/requests must carry a "
            "signed spend authorization, and signing is done by `potluck sign` plus "
            f"the consumer key. Sign the authorization payload with "
            f"`potluck sign --domain {AUTHORIZATION_DOMAIN} --key <consumer-key> "
            "--payload <authorization.json>`, assemble the signed record (schema, "
            "record_id, revision, signed_at, signer_key_id, signer_id, signature, "
            "payload) into a JSON file, and pass its path as --auth-file.",
            file=sys.stderr,
        )
        return 1
    consumer = load_consumer(args.config)
    authorization = _load_authorization(args.auth_file)
    result = consumer_post(
        consumer,
        "/v1/requests",
        {
            "idempotency_key": args.idempotency_key,
            "messages": [user_message(str(args.prompt)).to_payload()],
            "model": args.model,
            "delivery_mode": "buffered_complete",
            "max_attempts": 2,
            "deadline_ms": 60_000,
            "max_charge_milli_credits": args.max_charge,
            "authorization": authorization,
        },
    )
    request_id = str(result.get("request_id", ""))
    if not request_id:
        raise PotluckError(
            "the server accepted the request but returned no request_id; "
            f"response was {result!r}"
        )
    print(request_id)
    if not args.wait:
        return 0
    document = poll_request_until_terminal(consumer, request_id)
    print(canonicalize(document).decode("utf-8"))
    settlement = document.get("settlement")
    if isinstance(settlement, dict):
        print(
            f"{PROG}: receipt {settlement.get('receipt_id')} "
            f"(log index {settlement.get('log_index')}, hash {settlement.get('receipt_hash')})"
        )
    return 0 if document.get("status") == "SUCCEEDED" else 1


def _cmd_status(args: argparse.Namespace) -> int:
    """Run the ``status`` subcommand: the status document as canonical JSON."""
    consumer = load_consumer(args.config)
    document = consumer_get(consumer, f"/v1/requests/{args.request_id}")
    print(canonicalize(document).decode("utf-8"))
    return 0


def _cmd_events(args: argparse.Namespace) -> int:
    """Run the ``events`` subcommand: replayed events, then stop at ``done``."""
    if args.cursor < 0:
        raise PotluckError("--cursor must be a non-negative integer")
    consumer = load_consumer(args.config)
    frames = consumer_events(consumer, args.request_id, args.cursor)
    for frame in frames:
        print(canonicalize(frame.payload).decode("utf-8"))
    return 0


# --------------------------------------------------------------------------
# version
# --------------------------------------------------------------------------


def _package_version() -> str:
    """Return the installed distribution version, or the fallback constant.

    ``importlib.metadata`` reads the version from the installed ``ai-potluck``
    distribution's metadata, which is the version in ``pyproject.toml`` at build
    time. Running uninstalled from a source tree has no metadata, so the
    fallback keeps ``version`` working rather than raising.
    """
    try:
        return importlib.metadata.version(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return _FALLBACK_VERSION


def _cmd_version(args: argparse.Namespace) -> int:
    """Run the ``version`` subcommand."""
    del args
    print(f"{PROG} {_package_version()} (protocol {PROTOCOL_VERSION})")
    return 0


# --------------------------------------------------------------------------
# join: redeem an invite and save an identity
# --------------------------------------------------------------------------


def _config_dir(raw: Path | None) -> Path:
    """Return the resolved identity directory, defaulting to ``~/.aipotluck``."""
    base = raw if raw is not None else Path(DEFAULT_CONFIG_DIR)
    return base.expanduser()


def _write_secret_file(path: Path, text: str) -> None:
    """Write `text` to `path` with mode 0600, independent of the process umask.

    The descriptor is chmodded *before* the write, so a pre-existing file with a
    wider mode cannot briefly hold the secret while still world-readable.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _read_identity_key_file(path: Path) -> dict[str, Any]:
    """Read the local identity/key document.

    Only the key material is required; the identity fields (``key_id``,
    ``subject_id``) are server-derived and live in the config document, so a key
    file need not carry them.

    Raises:
        PotluckError: If the file is not a JSON object or misses a required
            field.
    """
    raw = read_text_file(path, what="identity key file")
    document = canonical.loads_strict(raw)
    if not isinstance(document, dict):
        raise PotluckError(f"identity key file {path} must be a JSON object")
    for field in ("public_key_b64", "private_key_b64"):
        value = document.get(field)
        if not isinstance(value, str) or not value:
            raise PotluckError(f"identity key file {path} is missing {field!r}")
    return document


def _save_node_token(directory: Path, token: str) -> Path:
    """Persist the server's one-time node-link token, returning the path written.

    The plaintext travels exactly once, in the ``POST /v1/join`` response: the
    server stores only its digest and cannot show it again. This file is
    therefore the sole copy, so it is written through
    :func:`_write_secret_file` (0600 regardless of umask) and its value is never
    printed by this module. A contributor who loses it recovers by having an
    administrator ``admin revoke-key`` the device key and re-joining with a
    fresh invite — there is no re-issue path, by design.
    """
    path = directory / _NODE_TOKEN_FILE
    _write_secret_file(path, canonicalize({"node_token": token}).decode("utf-8"))
    return path


def _load_saved_node_token(directory: Path) -> str | None:
    """Return the node-link token ``join`` saved here, or `None`.

    A missing, unreadable or malformed file yields `None` rather than raising.
    The caller has two other sources to try and one actionable error to give, so
    "this directory never joined as a node" is that error's job to explain —
    surfacing an I/O traceback here would hide the remedy behind the symptom.
    """
    path = directory / _NODE_TOKEN_FILE
    if not path.exists():
        return None
    try:
        document = canonical.loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, PotluckError):
        return None
    if not isinstance(document, dict):
        return None
    token = document.get("node_token")
    return token if isinstance(token, str) and token else None


def _read_saved_config(path: Path) -> dict[str, Any]:
    """Return a previously saved identity config, or ``{}``.

    Used to keep fields a re-join response does not repeat. ``join`` is
    idempotent on the public key, and the server's idempotent answer names the
    identity but not the device ids it derived on the first join — so a re-join
    that overwrote the file wholesale would silently drop the device binding and
    break the wizard that reads it back. Unreadable or malformed state is
    treated as absent: the fresh response is then the whole truth.
    """
    if not path.exists():
        return {}
    try:
        document = canonical.loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, PotluckError):
        return {}
    return document if isinstance(document, dict) else {}


def _post_json(url: str, body: dict[str, Any], *, timeout_s: float = 30.0) -> dict[str, Any]:
    """Send one unauthenticated canonical-JSON POST and return the parsed answer.

    Raises:
        PotluckError: If the URL is not absolute http(s), the transport fails,
            the answer is non-2xx, or the answer is not a JSON object.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PotluckError(f"server URL must be an absolute http(s) URL, not {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    payload = canonicalize(body)
    tls = client_ssl_context()
    connection = (
        http.client.HTTPSConnection(context=tls, host=parts.hostname, port=port, timeout=timeout_s)
        if parts.scheme == "https"
        else http.client.HTTPConnection(parts.hostname, port, timeout=timeout_s)
    )
    try:
        connection.request(
            "POST",
            target,
            body=payload,
            headers={"content-type": "application/json", "content-length": str(len(payload))},
        )
        response = connection.getresponse()
        raw = response.read()
    except OSError as exc:
        raise PotluckError(f"could not reach {parts.hostname}:{port}: {exc}") from exc
    finally:
        connection.close()
    if not 200 <= response.status < 300:
        detail = raw.decode("utf-8", "replace")[:300]
        raise PotluckError(f"POST {target} failed with HTTP {response.status}: {detail}")
    parsed = canonical.loads_strict(raw)
    if not isinstance(parsed, dict):
        raise PotluckError(f"POST {target} returned a non-object body")
    return parsed


def _cmd_join(args: argparse.Namespace) -> int:
    """Run the ``join`` subcommand: one command from an invite to a saved identity.

    The private key is generated once, written with mode 0600, and never printed.
    The identity itself (subject, key id, tenant) is **derived by the server**,
    so this command sends only the public key (and, for a node, the device
    public key) and stores the derived ids it gets back. Re-running with the
    saved key is idempotent: the server returns the same identity, funds nothing
    twice, mints no second node token, and does not consume another invite use.

    A node join additionally receives the platform link token the server minted
    for it and stores it in ``node_token.json`` (0600). That is what closes the
    contributor loop: the wizard reads the token back from there, so nobody has
    to copy a credential out of ``server.json`` by hand. The token is shown
    once by the server and never echoed here.

    A consumer join on an invite the operator minted ``--with-api-key``
    additionally receives an API key for the server's OpenAI-compatible
    endpoint, and that one *is* echoed — printed once, on its own line, with the
    ``base_url`` it belongs to and the warning that it will never be shown
    again. It is deliberately not written to a file: nothing this package runs
    needs it (``ask`` and ``serve-openai`` sign with the identity key), so
    storing it would only create a second place for it to be stolen from. Losing
    it costs an operator round trip, not the identity.
    """
    directory = _config_dir(args.config_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = directory / _KEY_FILE
    document = _read_identity_key_file(key_path) if key_path.exists() else None
    if document is None:
        public_key_b64, private_key_b64 = generate_key_pair()
        document = {
            "public_key_b64": public_key_b64,
            "private_key_b64": private_key_b64,
        }
    display_name = args.display_name or args.subject_id
    device: dict[str, str] | None = None
    if args.node:
        device_public = document.get("device_public_key_b64")
        device_private = document.get("device_private_key_b64")
        if not isinstance(device_public, str) or not isinstance(device_private, str):
            device_public, device_private = generate_key_pair()
            document["device_public_key_b64"] = device_public
            document["device_private_key_b64"] = device_private
        # Only the device public key is sent; the server derives device_id,
        # device_key_id and provider_id from this join's token.
        device = {"public_key_b64": device_public}
    _write_secret_file(key_path, json.dumps(document))
    server = args.server.rstrip("/")
    result = _post_json(
        f"{server}/v1/join",
        {
            "invite_code": args.invite,
            "identity": {
                "public_key_b64": document["public_key_b64"],
                "display_name": display_name,
            },
            "device": device,
        },
    )
    config_document = {
        "server_url": args.server,
        "key_id": result["key_id"],
        "subject_id": result["subject_id"],
        "tenant_id": result["tenant_id"],
        "role": result["role"],
    }
    config_path = directory / _CONFIG_FILE
    previous = _read_saved_config(config_path)
    for field in ("device_id", "device_key_id", "provider_id"):
        # An idempotent re-join answers with the identity but not the device ids
        # the first join derived, so a value already on disk is kept rather than
        # dropped: losing the device binding here would break `share setup`.
        value = result.get(field) or previous.get(field)
        if isinstance(value, str) and value:
            config_document[field] = value
    config_path.write_text(json.dumps(config_document), encoding="utf-8")
    print(
        f"{PROG}: joined as {result['subject_id']} (role {result['role']}, "
        f"tenant {result['tenant_id']}, key {result['key_id']}); identity saved to {directory}"
    )
    node_token = result.get("node_token")
    if isinstance(node_token, str) and node_token:
        token_path = _save_node_token(directory, node_token)
        print(
            f"{PROG}: platform node token saved to {token_path} (0600, never printed); "
            "the server keeps only its digest, so if it is lost ask the operator to run "
            "`admin revoke-key` and re-join with a fresh invite"
        )
    api_key = result.get("api_key")
    if isinstance(api_key, str) and api_key:
        # The key on its own line, so `potluck join … | grep '^ak-'` is a
        # supported way to hand it to a tool, and the wording that explains it
        # under it. Printed, never stored: see this command's docstring.
        print(api_key)
        print(f"{PROG}: {ux.joined_api_key_text(base_url=f'{server}/openai/v1')}")
    if args.node and not args.no_setup:
        # A node is only useful once it has an upstream and has proven it can
        # reach it, so the wizard runs right here rather than being a command
        # the operator has to know to run next. The identity is already saved,
        # so an interrupted wizard leaves a resumable state (`share setup`).
        return _run_contributor_wizard(
            args,
            directory=directory,
            identity_config=config_document,
            key_document=document,
        )
    return 0


# --------------------------------------------------------------------------
# ask: the one-command consumer path
# --------------------------------------------------------------------------


def load_joined_identity(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the saved identity's config and key documents.

    Raises:
        PotluckError: If either file is absent or malformed, the config
            carries a key listed in ``RENAMED_CONFIG_KEYS``, or it carries a
            key outside ``IDENTITY_CONFIG_KEYS``.
    """
    config_path = directory / _CONFIG_FILE
    key_path = directory / _KEY_FILE
    if not config_path.exists() or not key_path.exists():
        raise PotluckError(
            f"no identity found in {directory}; run "
            f"`{PROG} join --server <url> --invite <code>` first"
        )
    raw = read_text_file(config_path, what="identity config")
    config_document = canonical.loads_strict(raw)
    if not isinstance(config_document, dict):
        raise PotluckError(f"identity config {config_path} must be a JSON object")
    reject_renamed_keys(config_document, f"identity config {config_path}")
    reject_unknown_keys(
        config_document, IDENTITY_CONFIG_KEYS, f"identity config {config_path}"
    )
    for field in ("server_url", "key_id", "subject_id", "tenant_id", "role"):
        value = config_document.get(field)
        if not isinstance(value, str) or not value:
            raise PotluckError(f"identity config {config_path} is missing {field!r}")
    return config_document, _read_identity_key_file(key_path)


def signed_client_for(
    config_document: dict[str, Any], key_document: dict[str, Any]
) -> SignedClient:
    """Build a `SignedClient` from a saved identity's two documents.

    One construction site for the signed consumer, shared by ``ask``,
    ``doctor``, ``verify receipt`` and :mod:`aipotluck.localapi`: the fields an
    identity carries are read in exactly one place, so a change to what a join
    saves cannot leave one caller authenticating differently from the others.
    """
    return SignedClient(
        server_url=str(config_document["server_url"]),
        key_id=str(config_document["key_id"]),
        private_key_b64=str(key_document["private_key_b64"]),
    )


def _signed_record_document(record: Any) -> dict[str, Any]:
    """Render a signed record as the JSON object a client POSTs."""
    return {
        "schema": record.schema,
        "record_id": record.record_id,
        "revision": record.revision,
        "signed_at": record.signed_at.isoformat(),
        "signer_key_id": record.signer_key_id,
        "signer_id": record.signer_id,
        "signature": record.signature,
        "payload": dict(record.payload),
        "previous_record_hash": record.previous_record_hash,
    }


def build_signed_request(
    *,
    config_document: dict[str, Any],
    key_document: dict[str, Any],
    messages: tuple[ChatMessage, ...],
    model: str,
    parameters: Mapping[str, Any],
    max_charge: int,
    delivery_mode: str,
    deadline_ms: int = 60_000,
    session_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build a signed authorization and the request body the server expects.

    The `request_digest` is computed by `compute_request_digest` from the exact
    ``messages``/``model``/``delivery_mode``/``parameters`` submitted, which the
    server re-derives at admission; a mismatch is refused, so the two must be
    built from the same values. That is also why ``delivery_mode`` is a parameter
    rather than a constant: the signature has to cover the mode the body actually
    asks for.

    This is the one place a consumer request is minted, and both consumers of it
    — ``ask``, which builds one user turn from a question, and
    :mod:`aipotluck.localapi`, which forwards a tool's own conversation — drive
    the same signature, submission and wait. A second copy is how the two paths
    would drift apart about what a request costs.

    Args:
        config_document: The identity config a ``join`` saved.
        key_document: The identity key file; only its private key is read.
        messages: The conversation, forwarded to a contribution node verbatim.
        model: The model requested; the authorization is scoped to it alone.
        parameters: The forwarded parameters, in the canonical-safe encoding
            :func:`aipotluck.wire.encode_chat_parameters` produces. They are
            part of the digest, so a ceiling signed for one parameter set cannot
            be spent on another.
        max_charge: The charge ceiling in milli-credits.
        delivery_mode: The delivery mode the body asks for.
        deadline_ms: The server-side deadline the body carries.
        session_id: The agent session this turn belongs to, or `None` for a
            request that belongs to no session. Deliberately **not** part of the
            digest: a session is a label the server aggregates by, not work a
            node executes, so signing over it would make a caller re-sign to move
            one turn between its own sessions while buying nothing — the label
            can only ever attribute a request to a session of the same
            authenticated subject, which is the credential's business and not the
            body's.

    Returns:
        The minted request id, and the body to POST to ``/v1/requests``.

    Raises:
        ProtocolError: If `session_id` is not an identifier the wire accepts.
    """
    if session_id is not None:
        check_session_id(session_id)
    request_id = "req-" + secrets.token_hex(16)
    now = dt.datetime.now(dt.UTC)
    subject_id = str(config_document["subject_id"])
    # The signing key id is the one the *server* derived at join; the local key
    # file no longer chooses it, so it is read from the saved config.
    key_id = str(config_document["key_id"])
    request_digest = compute_request_digest(
        messages=messages,
        model=model,
        delivery_mode=delivery_mode,
        parameters=parameters,
    )
    authorization = SpendAuthorization(
        authorization_id=f"auth-{request_id}",
        payer_account_id=subject_id,
        consumer_id=subject_id,
        request_id=request_id,
        request_digest=request_digest,
        max_charge_milli_credits=max_charge,
        pricing_version="flat-v1",
        billing_basis="reported_native_usage",
        minimum_evidence_level="node_reported",
        allowed_models=frozenset({model}),
        allowed_trust_pools=frozenset({"trusted-beta"}),
        max_attempts=2,
        duplicate_execution_policy="bounded_model_only",
        max_provider_fanout=2,
        nonce=new_nonce(),
        issued_at=now,
        expires_at=now + dt.timedelta(minutes=10),
        key_id=key_id,
    )
    signed = authorization.sign(
        load_private_key(str(key_document["private_key_b64"])), key_id
    )
    body = {
        "idempotency_key": "idem-" + secrets.token_hex(16),
        "messages": [message.to_payload() for message in messages],
        "model": model,
        "parameters": dict(parameters),
        "delivery_mode": delivery_mode,
        "max_attempts": 2,
        "deadline_ms": deadline_ms,
        "max_charge_milli_credits": max_charge,
        "authorization": _signed_record_document(signed),
    }
    if session_id is not None:
        body["session_id"] = session_id
    return request_id, body


class NoAvailableNodes(PotluckError):
    """Raised when the server has no node able to run the requested model.

    A distinct type rather than a message match: ``ask`` reacts to this by
    cancelling the request it already submitted, which it must not do for a
    plain timeout (the work may still be progressing).
    """


class WaitDeadline(PotluckError):
    """Raised when a wait window closed while the request was still unfinished.

    A distinct type rather than a message match, for the same reason as
    `NoAvailableNodes`: a deadline is not a refusal. The work may still be
    running at the server and may still be billed, so
    :mod:`aipotluck.localapi` answers it as ``504 deadline_passed`` with the
    billing-honest message, while a server refusal is answered with the code the
    server itself named. Telling those apart by parsing a message is how a
    request that may still cost something comes to be reported as a failure of
    the platform.
    """


def _print_billing_note(document: dict[str, Any]) -> None:
    """Print the server's own one-line reason for a request that did not succeed.

    Why this exists: a FAILED request used to render as "did not finish" and
    nothing else, while the server had already written the actual cause — ``no
    node left to take the work: local budget would be exceeded: 0 + 20000 >
    5000`` — into ``requests.billing_note`` and returned it in the status
    document. Swallowing it turned a diagnosable configuration mismatch into an
    unexplained failure.

    The note is printed verbatim rather than classified or paraphrased: it is
    the server's wording, and a second rendering of it would be a second source
    of truth about why a request failed. It is assembled from request and
    attempt ids, numbers, and node-reported failure text, never from a
    credential, so surfacing it does not widen what leaves the server.
    """
    note = document.get("billing_note")
    if isinstance(note, str) and note:
        print(f"{PROG}: server note: {note}", file=sys.stderr)


def _model_nodes(client: SignedClient, model: str) -> list[dict[str, Any]]:
    """Return the connected nodes that advertise `model`.

    Reads the authenticated ``GET /v1/nodes`` view. The filter is local because
    the endpoint lists every connected node; the client only cares about nodes
    that could actually take this request.

    Raises:
        PotluckError: If the listing cannot be read, or the server answered
            with something other than a ``nodes`` array. An unreadable listing
            says nothing about whether a node exists, so it is never flattened
            into "there are none": the callers treat an empty answer as "no
            node can run this" and end the request over it, which is only ever
            true of a listing that was actually read.
    """
    document = client.get("/v1/nodes")
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        raise PotluckError(
            "server returned a malformed node listing: 'nodes' is not an array"
        )
    return [
        node
        for node in nodes
        if isinstance(node, dict) and node.get("model") == model and node.get("share_enabled")
    ]


def _default_model_from_nodes(client: SignedClient) -> str | None:
    """Return the model to request when ``--model`` was omitted, or `None`.

    The rule is the majority of online, sharing nodes, ties broken by the
    alphabetically smallest name. Majority rather than "the first one seen"
    because a pool of contributors mostly serving the same model is what a
    consumer means by "the platform's model", and an arbitrary pick would send
    the request to a node that may be the only one able to take it. The tie-break
    is what makes the choice deterministic: two equally common models must not
    depend on the order a listing happened to arrive in.

    Only nodes that could actually take the work are counted — the same
    ``share_enabled`` filter :func:`_model_nodes` and the server's own
    ``_pick_node`` apply — so a paused node cannot contribute a model that then
    cannot be dispatched.

    Returns:
        The model name, or `None` when the listing was read and no online node
        advertised a model.

    Raises:
        PotluckError: If the listing itself could not be read. Propagated rather
            than flattened into `None` on purpose: an unreadable listing says
            nothing about whether a node exists, and reporting it as "no model"
            would hide the real cause — a revoked credential or an unreachable
            server has its own remedy, and masking it as an empty platform is
            how a user ends up waiting for contributors instead of re-joining.
    """
    document = client.get("/v1/nodes")
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        return None
    counts: dict[str, int] = {}
    for node in nodes:
        if not isinstance(node, dict) or not node.get("share_enabled"):
            continue
        model = node.get("model")
        if isinstance(model, str) and model:
            counts[model] = counts.get(model, 0) + 1
    if not counts:
        return None
    return min(counts, key=lambda name: (-counts[name], name))


def _await_terminal(
    client: SignedClient,
    request_id: str,
    *,
    model: str,
    timeout_s: float,
    no_node_grace_s: float,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Poll a request until it settles, turns terminal, or has no node left.

    Two bounds, not one. `timeout_s` is the overall budget; `no_node_grace_s`
    is the much shorter "nothing can run this, and nothing is coming" bound. A
    request with no eligible node sits in ``ROUTING`` until the server's
    deadline, so waiting out `timeout_s` would mean a user staring at a
    two-minute silence for a request that provably cannot start. The node list
    is consulted each poll, and the grace only starts once the list is empty,
    so a node that appears late still gets its chance. The list is only ever
    consulted while the request is pre-dispatch — see
    `PRE_DISPATCH_REQUEST_STATES` — because a request a node is already
    executing is billed work no listing may end.

    Args:
        client: The signed consumer.
        request_id: The request to watch.
        model: The model, for the node check.
        timeout_s: Overall budget in seconds.
        no_node_grace_s: How long an empty node list is tolerated.
        cancel_event: Set by a signal handler to request cancellation.

    Returns:
        The terminal (or settled) status document.

    Raises:
        NoAvailableNodes: If no node advertised the model for the whole grace.
        WaitDeadline: If the wait window closed with the request unfinished.
        PotluckError: If the user cancelled, or a read of the server failed.
    """
    deadline = time.monotonic() + timeout_s
    empty_since: float | None = None
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise PotluckError(f"cancelled by the user while waiting for {request_id}")
        document = client.get(f"/v1/requests/{request_id}")
        if document.get("settlement") is not None or (
            document.get("status") in TERMINAL_REQUEST_STATES
        ):
            return document
        now = time.monotonic()
        if document.get("status") not in PRE_DISPATCH_REQUEST_STATES:
            # A node already holds an attempt: it is demonstrably serving this
            # request, so the listing is not even consulted. A request past the
            # pre-dispatch states is billed work, and a node that pauses
            # mid-flight must never be mistaken for a node that was never
            # there — ending the request here would cancel a real charge.
            empty_since = None
        elif _model_nodes(client, model):
            empty_since = None
        elif empty_since is None:
            empty_since = now
        elif now - empty_since >= no_node_grace_s:
            raise NoAvailableNodes(
                f"{ux.NO_NODES_PHRASE} (waited {no_node_grace_s:g}s and still no node "
                f"can run model {model!r})"
            )
        if now >= deadline:
            raise WaitDeadline(
                f"request {request_id} did not reach a terminal state within "
                f"{timeout_s:g}s; last status {document.get('status')!r}"
            )
        time.sleep(0.2)


def _posting_delta(value: Any) -> int | None:
    """Read one receipt posting amount, or `None` if it is not a number.

    Receipts are canonical JSON, and the canonical form writes integers as
    decimal *strings* so a large amount cannot silently lose precision to a
    float. So a posting arrives as ``"-1000"``, not ``-1000``. Reading only the
    integer case therefore matched nothing and reported a charge of zero for
    every request — the single wrong answer this cost line exists to avoid.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError:
            return None
    return None


def _cost_line(client: SignedClient, document: dict[str, Any], subject_id: str) -> str:
    """Return a one-line cost summary for a finished request.

    A request with no settlement revision is reported through
    :func:`aipotluck.ux.uncertain_usage_cost_text`, never as a charge of zero:
    "we have not been told the price" and "the price is zero" are different
    statements, and only one of them is a claim the server can support.

    The same reasoning covers a settled request that did not succeed. A
    cancelled or failed request is settled provisionally — the model may have
    been stopped mid-answer, and a cancellation can leave a receipt whose
    postings are already voided — so the number in it is not the price of a
    delivered answer. Printing it would put "cost 0" in front of a user whose
    upstream really did burn tokens, which reads as "this was free". The amount
    is therefore reported as still to be checked, together with the receipt
    where the final figure will appear.
    """
    settlement = document.get("settlement")
    if not isinstance(settlement, dict):
        return ux.uncertain_usage_cost_text()
    receipt_id = str(settlement.get("receipt_id", ""))
    if str(document.get("status", "")) != "SUCCEEDED":
        return ux.uncertain_usage_cost_text(receipt_id=receipt_id or None)
    try:
        receipt_document = client.get(f"/v1/receipts/{receipt_id}")
    except PotluckError:
        return f"cost: settled (receipt {receipt_id}; amount to be confirmed)"
    record = receipt_document.get("receipt")
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        return f"cost: settled (receipt {receipt_id}; amount to be confirmed)"
    postings = payload.get("postings")
    if not isinstance(postings, list):
        return ux.uncertain_usage_cost_text(receipt_id=receipt_id or None)
    charge = 0
    posted = False
    for posting in postings:
        if not isinstance(posting, dict) or posting.get("account") != subject_id:
            continue
        delta = _posting_delta(posting.get("delta"))
        if delta is None:
            continue
        charge -= delta
        posted = True
    if not posted:
        # The receipt says it settled, but it carries no line for this payer.
        # That is not the same as a price of zero, so do not invent one.
        return ux.uncertain_usage_cost_text(receipt_id=receipt_id or None)
    return (
        f"cost {charge} milli-credit "
        f"({payload.get('billable_tokens')} tokens, receipt {receipt_id})"
    )


class AnswerPresenter(Protocol):
    """The single seam an answer is rendered through.

    ``ask`` has two implementations (a terminal one and a JSON one) and
    :mod:`aipotluck.localapi` has two more (one that collects an answer into a
    tool's document, one that writes each increment to a tool's stream). All four
    are driven by the same three calls from the same two wait loops. That is
    deliberate: the network loop must not contain ``print`` calls, or the next
    change to the display becomes a change to the code that talks to the network.
    """

    def status(self, text: str) -> None:
        """Report a human status line (for example "generating")."""
        ...

    def delta(self, text: str) -> None:
        """Report one increment of the answer, in order."""
        ...

    def finish(self, *, answer: str, cost: str, status: str, request_id: str) -> None:
        """Report the completed answer and its cost line."""
        ...


class _TextPresenter:
    """Prints statuses on stderr and increments inline on stdout."""

    def __init__(self) -> None:
        """Create a presenter that has printed nothing yet."""
        self._last_status: str | None = None
        self._printed_any = False

    def status(self, text: str) -> None:
        """Print a status line, skipping an immediate repeat."""
        if text and text != self._last_status:
            self._last_status = text
            print(f"{PROG}: {text}", file=sys.stderr)

    def delta(self, text: str) -> None:
        """Write one increment without a newline, flushing so it is visible now."""
        if not text:
            return
        self._printed_any = True
        sys.stdout.write(text)
        sys.stdout.flush()

    def finish(self, *, answer: str, cost: str, status: str, request_id: str) -> None:
        """Close the answer (a newline if increments were printed) and the cost."""
        del request_id
        if self._printed_any:
            sys.stdout.write("\n")
        else:
            # Nothing was streamed: print the whole answer in one piece, which
            # is what "buffered" means and must not be dressed up as streaming.
            print(answer)
        sys.stdout.flush()
        print(f"{PROG}: {ux.describe_request(status)}")
        print(f"{PROG}: {cost}")


class _JsonPresenter:
    """Collects the answer and prints exactly one machine-readable document."""

    def __init__(self) -> None:
        """Create a presenter with an empty accumulator."""
        self._parts: list[str] = []

    def status(self, text: str) -> None:
        """Ignore statuses: a JSON consumer reads the final document only."""
        del text

    def delta(self, text: str) -> None:
        """Accumulate one increment without printing it."""
        self._parts.append(text)

    def finish(self, *, answer: str, cost: str, status: str, request_id: str) -> None:
        """Print one canonical JSON document describing the whole result."""
        print(
            canonicalize(
                {
                    "request_id": request_id,
                    "status": status,
                    "status_text": ux.describe_request(status),
                    "answer": answer or "".join(self._parts),
                    "summary": cost,
                }
            ).decode("utf-8")
        )


def _presenter_for(*, json_mode: bool) -> AnswerPresenter:
    """Return the presenter matching the requested output format."""
    return _JsonPresenter() if json_mode else _TextPresenter()


def post_cancel(client: SignedClient, request_id: str) -> bool:
    """Ask the server to cancel a request, returning whether it took.

    Cancellation is a real end-to-end action, not a local exit: the server marks
    the request and pushes ``attempt.cancel`` to the node running it, so the
    upstream call stops instead of billing to the end.
    """
    try:
        client.post(f"/v1/requests/{request_id}/cancel", {})
    except PotluckError:
        return False
    return True


def _install_cancel_handler(cancel_event: threading.Event) -> list[tuple[int, Any]] | None:
    """Install SIGINT/SIGTERM handlers that set `cancel_event`.

    Returns the previous handlers so the caller can restore them: a library
    caller of `main` must not find its process's signal disposition permanently
    changed by running an ``ask``. Returns `None` when the caller is not the
    main thread, where the OS forbids installing handlers.

    The handler only sets an event. It deliberately does not POST the cancel
    itself: doing I/O in a signal handler risks re-entering the very socket
    write the interrupt should be ending. The streaming loop, which runs at
    bytecode speed, performs the cancel.
    """
    def _handle(signum: int, _frame: Any) -> None:
        del signum  # one event covers both signals
        cancel_event.set()

    previous: list[tuple[int, Any]] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous.append((signum, signal.signal(signum, _handle)))
        except ValueError:
            return None
    return previous


def _restore_handlers(previous: list[tuple[int, Any]] | None) -> None:
    """Restore the signal handlers `_install_cancel_handler` replaced."""
    if previous is None:
        return
    for signum, handler in previous:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signum, handler)


def _write_pending(path: Path, document: dict[str, Any]) -> None:
    """Persist an in-flight request so `--resume` can finish it."""
    path.write_text(canonicalize(document).decode("utf-8"), encoding="utf-8")


def _load_pending(path: Path) -> dict[str, Any]:
    """Load the in-flight request document.

    Raises:
        PotluckError: If there is no pending request or it is malformed.
    """
    if not path.exists():
        raise PotluckError("no pending request to resume; run `potluck ask ...` first")
    raw = read_text_file(path, what="pending request")
    document = canonical.loads_strict(raw)
    if not isinstance(document, dict) or not isinstance(document.get("body"), dict):
        raise PotluckError(f"pending request {path} is malformed")
    return document


def _record_error(directory: Path, guidance: ux.FailureGuidance) -> None:
    """Persist the last failure's machine code for ``doctor`` to report.

    Only the code, the family and the instant are stored — never the message.
    A message can quote a URL, a request id, or a server-rendered detail, and a
    file that exists to be read back by a diagnostic command must not become a
    place a credential can accumulate. Best-effort: a read-only config
    directory must not turn a failed ``ask`` into a crash.
    """
    document = {
        "code": str(guidance.kind),
        "phrase": guidance.phrase,
        "at": dt.datetime.now(dt.UTC).isoformat(),
    }
    with contextlib.suppress(OSError):
        _write_secret_file(
            directory / _LAST_ERROR_FILE, canonicalize(document).decode("utf-8")
        )


def run_buffered_answer(
    client: SignedClient,
    request_id: str,
    presenter: AnswerPresenter,
    *,
    model: str,
    timeout_s: float,
    no_node_grace_s: float,
    cancel_event: threading.Event,
) -> tuple[str, bool, list[str]]:
    """Wait for a terminal status, then present the whole answer at once.

    This is the path for ``buffered_complete`` and for a JSON consumer, and the
    one :mod:`aipotluck.localapi` drives for a tool's non-streaming request. It
    shows a single explicit "waiting" notice and then the complete answer:
    calling that streaming would be a lie about how the answer was delivered.

    Returns:
        ``(status, cancelled, answer_parts)``.

    Raises:
        NoAvailableNodes: If no node advertised the model for the whole grace.
        WaitDeadline: If the wait window closed with the request unfinished.
        PotluckError: On a transport failure, or a cancellation.
    """
    presenter.status("waiting for generation...")
    document = _await_terminal(
        client,
        request_id,
        model=model,
        timeout_s=timeout_s,
        no_node_grace_s=no_node_grace_s,
        cancel_event=cancel_event,
    )
    frames = client.events(request_id, 0)
    answer = "".join(
        str(frame.payload.get("text", "")) for frame in frames if frame.event == "output.delta"
    )
    return str(document.get("status", "")), cancel_event.is_set(), [answer]


def run_streaming_answer(
    client: SignedClient,
    request_id: str,
    presenter: AnswerPresenter,
    *,
    model: str,
    timeout_s: float,
    no_node_grace_s: float,
    cancel_event: threading.Event,
) -> tuple[str, bool, list[str]]:
    """Consume the server's event stream and present each increment as it arrives.

    How the pieces fit, and why:

    * The stream is read through `LiveEventStream`, which reads the socket on a
      daemon thread. The loop here therefore stays responsive: it can honour its
      deadline, notice Ctrl-C, and check for an eligible node, none of which a
      blocking ``readline`` would allow.
    * A dropped stream is *re-opened from the last cursor*, never re-posted.
      The server replays only events after the cursor, so a reconnect duplicates
      neither text nor charge — which is exactly why the cursor has to be
      tracked here rather than inferred from what was printed.
    * Increments are admitted only from one attempt id (the first that produced
      output). The server already refuses to store a superseded branch's
      deltas, so this is belt-and-braces against ever splicing two answers.
    * Ctrl-C sets the cancel flag; this loop then POSTs the cancel and keeps
      reading until the terminal frame, so the user sees the real end state
      rather than a local exit.

    The loop is written once and drives whichever `presenter` it is given, which
    is how :mod:`aipotluck.localapi` streams to a tool: a second copy of the
    reconnect-from-cursor logic is how a stream ends up splicing two answers
    together.

    Returns:
        ``(status, cancelled, answer_parts)``.
    """
    deadline = time.monotonic() + timeout_s
    cursor = 0
    answer_parts: list[str] = []
    answer_attempt: str | None = None
    status = ""
    seen_status: str | None = None
    cancel_sent = False
    cancel_deadline: float | None = None
    next_check = 0.0
    empty_since: float | None = None
    no_node = False

    def publish(current: str) -> None:
        nonlocal seen_status
        if current and current != seen_status:
            seen_status = current
            presenter.status(ux.describe_request(current))

    def admit(frame: SseFrame) -> bool:
        """Fold one frame into the answer and say whether it was the terminal one.

        Both the live reader and the post-drop drain go through here, and that
        is deliberate. The cursor advance, the "only the attempt that produced
        the first increment may contribute text" rule, and the terminal
        detection are each easy to get subtly wrong once, let alone twice; a
        second copy of them in the drain path is how a reconnect ends up
        splicing two answers together.

        Frames at or below the cursor are skipped: a reconnect replays a frame
        the consumer may already have seen just before the drop, and showing it
        again would duplicate a character the user has already paid for.
        """
        nonlocal cursor, answer_attempt, status
        if frame.cursor is not None:
            if frame.cursor <= cursor:
                return False
            cursor = frame.cursor
        if frame.event == "output.delta":
            text = str(frame.payload.get("text", ""))
            attempt = frame.payload.get("attempt_id")
            if not text:
                return False
            if answer_attempt is None:
                answer_attempt = attempt if isinstance(attempt, str) else None
                publish("RUNNING")
            elif attempt != answer_attempt:
                # Another branch of the same request: its text must never be
                # spliced onto the answer the user is reading.
                return False
            answer_parts.append(text)
            presenter.delta(text)
            return False
        if frame.event == "done":
            status = str(frame.payload.get("status", status))
            return True
        return False

    initial = client.get(f"/v1/requests/{request_id}")
    status = str(initial.get("status", ""))
    publish(status)

    terminal = False
    while not terminal:
        now = time.monotonic()
        if cancel_event.is_set() and not cancel_sent:
            # One attempt, then give up: a cancel that cannot be delivered must
            # not spin, and the local deadline still bounds the wait.
            cancel_sent = True
            cancel_deadline = now + ASK_CANCEL_GRACE_S
            if post_cancel(client, request_id):
                publish("CANCELLED")
        if now >= deadline:
            break
        if cancel_deadline is not None and now >= cancel_deadline:
            break
        stream: LiveEventStream = client.open_event_stream(request_id, cursor)
        stream.start()
        dropped = False
        try:
            while True:
                frame = stream.poll(ASK_STREAM_POLL_S)
                if frame is not None:
                    if admit(frame):
                        terminal = True
                        break
                    continue
                if stream.ended:
                    # The connection died mid-answer, or the server closed a
                    # live request's stream. Whatever was buffered locally is
                    # gone, but nothing is lost: every event is stored at the
                    # server. Reconnecting from `cursor` replays only what has
                    # not been shown, which is what keeps the text from
                    # repeating and the charge from being taken twice.
                    dropped = True
                    break
                now = time.monotonic()
                if cancel_event.is_set() and not cancel_sent:
                    break
                if now >= deadline or (
                    cancel_deadline is not None and now >= cancel_deadline
                ):
                    break
                if now >= next_check:
                    next_check = now + 1.0
                    document = client.get(f"/v1/requests/{request_id}")
                    status = str(document.get("status", status))
                    publish(status)
                    # The node check only matters before anything is running:
                    # once an attempt exists, a node is demonstrably serving it,
                    # and a node that pauses mid-flight must not be mistaken for
                    # a node that was never there.
                    if status in PRE_DISPATCH_REQUEST_STATES:
                        if _model_nodes(client, model):
                            empty_since = None
                        elif empty_since is None:
                            empty_since = now
                        elif now - empty_since >= no_node_grace_s:
                            no_node = True
                            break
        finally:
            stream.close()
        if no_node:
            raise NoAvailableNodes(
                f"{ux.NO_NODES_PHRASE} (waited {no_node_grace_s:g}s and still no node "
                f"can run model {model!r})"
            )
        if terminal:
            break
        if dropped:
            # A die-off and a finished request look identical from here, so ask
            # the server which it was. Only a *terminal* request may be drained
            # with a plain read: reading a live request's events blocks until it
            # ends, which would quietly turn a stream back into a buffer and
            # stop showing increments. A live one is re-opened from the cursor
            # below instead, so the answer keeps arriving in pieces.
            document = client.get(f"/v1/requests/{request_id}")
            status = str(document.get("status", status))
            publish(status)
            if status in TERMINAL_REQUEST_STATES:
                for frame in client.events(request_id, cursor):
                    if admit(frame):
                        terminal = True
                        break
                break
        if cancel_deadline is not None and time.monotonic() >= cancel_deadline:
            break
        if time.monotonic() >= deadline:
            break
        # A short pause before reconnecting keeps a server that is refusing
        # connections from being hammered, while staying responsive.
        time.sleep(ASK_RECONNECT_PAUSE_S)
    return status, cancel_event.is_set(), answer_parts


def _report_ask_failure(
    directory: Path, exc: PotluckError, *, status_document: dict[str, Any] | None = None
) -> int:
    """Record one ``ask`` failure and print it with its classified next step.

    Shared by every path that ends an ``ask`` in a handled error, so the message
    a user reads does not depend on which step happened to fail. Returns the
    exit status rather than raising: ``ask`` reports and exits 1, it does not
    propagate.

    ``status_document`` is the request as the server sees it, when the caller
    could read one back. Its note is printed between the local error and the next
    step because the two are different facts: the error says how this wait ended,
    the note says why the server had not finished.
    """
    guidance = ux.classify_failure(
        error_code=getattr(exc, "error_code", None),
        http_status=getattr(exc, "http_status", None),
        message=str(exc),
    )
    _record_error(directory, guidance)
    print(f"{PROG}: error: {exc}", file=sys.stderr)
    if status_document is not None:
        _print_billing_note(status_document)
    print(f"{PROG}: {guidance.next_step}", file=sys.stderr)
    return 1


def _cmd_ask(args: argparse.Namespace) -> int:
    """Run the ``ask`` subcommand: sign, dispatch, wait, and print the answer.

    Two delivery modes, chosen explicitly and never conflated:

    * ``--stream`` asks the server for ``stream_strict`` and prints each
      increment the moment its frame arrives.
    * otherwise the request is ``buffered_complete`` and the whole answer is
      printed once, after an explicit "waiting" notice.

    The request is re-submitted on ``--resume`` rather than assumed to exist:
    the idempotency key makes that a no-op if the original POST already landed,
    and it recovers the window where the process died between writing
    ``pending.json`` and the POST completing.

    Two things this command refuses to guess. Without ``--model`` the model is
    read off the online nodes rather than defaulted to a name baked into the
    client, and no request is created at all when there is no node to read one
    from. And when the server ends a request in a failure state, its own
    ``billing_note`` is printed: the reason a request failed lives at the
    server, and hiding it behind "did not finish" is what makes a default-config
    mismatch look like a mysterious outage.
    """
    directory = _config_dir(args.config_dir)
    config_document, key_document = load_joined_identity(directory)
    subject_id = str(config_document["subject_id"])
    client = signed_client_for(config_document, key_document)
    pending_path = directory / _PENDING_FILE
    requested_mode = "stream_strict" if args.stream else "buffered_complete"
    if args.resume:
        pending = _load_pending(pending_path)
        body = pending["body"]
        request_id = str(pending["request_id"])
        # A resumed request keeps the mode it was admitted with: the authorization
        # signed that digest, so switching modes here would be refused anyway, and
        # presenting a buffered request as a stream is the one thing the display
        # must not do.
        stored_mode = body.get("delivery_mode")
        stream_display = stored_mode == "stream_strict"
        # The same reasoning fixes the model: the node-availability check has to
        # look for the capability this request actually named, not for whatever
        # the online nodes happen to serve now.
        model = str(body.get("model") or "")
    else:
        explicit = args.model
        model = str(explicit) if explicit else ""
        if not model:
            try:
                inferred = _default_model_from_nodes(client)
            except PotluckError as exc:
                return _report_ask_failure(directory, exc)
            if inferred is None:
                # Nothing online to infer a model from, so nothing could route
                # this request. Refusing *before* creating it is the point: a
                # request that provably cannot start should not be queued, given
                # a nonce, and then cancelled — and the user gets a next step
                # instead of an open-ended wait.
                _record_error(directory, ux.guidance_for(ux.FailureKind.NO_NODES))
                print(f"{PROG}: {ux.NO_MODEL_PHRASE}", file=sys.stderr)
                print(f"{PROG}: {ux.NO_MODEL_NEXT_STEP}", file=sys.stderr)
                return 1
            model = inferred
            print(
                f"{PROG}: no --model given; using model {model} advertised by the "
                "online nodes",
                file=sys.stderr,
            )
        request_id, body = build_signed_request(
            config_document=config_document,
            key_document=key_document,
            # One question is one user turn: the request model the server and the
            # node speak is a conversation, never a flattened prompt string.
            messages=(user_message(args.question),),
            model=model,
            # `ask` names no parameter, so the upstream's own defaults apply.
            parameters={},
            max_charge=args.max_charge,
            delivery_mode=requested_mode,
            deadline_ms=int(args.timeout) * 1000,
        )
        _write_pending(
            pending_path,
            {
                "request_id": request_id,
                "body": body,
                "created_at": dt.datetime.now(dt.UTC).isoformat(),
            },
        )
        stream_display = args.stream

    presenter = _presenter_for(json_mode=args.json_output)
    cancel_event = threading.Event()
    handlers = _install_cancel_handler(cancel_event)
    try:
        result = client.post("/v1/requests", body)
        returned = result.get("request_id")
        if isinstance(returned, str) and returned:
            request_id = returned
        if stream_display and not args.json_output:
            status, cancelled, parts = run_streaming_answer(
                client,
                request_id,
                presenter,
                model=model,
                timeout_s=args.timeout,
                no_node_grace_s=args.no_node_grace,
                cancel_event=cancel_event,
            )
        else:
            status, cancelled, parts = run_buffered_answer(
                client,
                request_id,
                presenter,
                model=model,
                timeout_s=args.timeout,
                no_node_grace_s=args.no_node_grace,
                cancel_event=cancel_event,
            )
    except NoAvailableNodes as exc:
        # Nothing can run this, so do not leave it queued: cancel it and say so.
        post_cancel(client, request_id)
        _record_error(directory, ux.guidance_for(ux.FailureKind.NO_NODES))
        print(f"{PROG}: {exc}", file=sys.stderr)
        # The server may already have written down *why* nothing took the work —
        # a node that declined on its local budget, say. Read it back rather
        # than leaving the user with only "no node".
        with contextlib.suppress(PotluckError):
            _print_billing_note(client.get(f"/v1/requests/{request_id}"))
        print(f"{PROG}: {ux.NO_NODES_NEXT_STEP}", file=sys.stderr)
        return 1
    except WaitDeadline as exc:
        # The local wait closed; the work did not. The server may still be holding
        # the request in its dispatch queue or running it on a node, so nothing is
        # cancelled here and `--resume` stays available. What the user is owed
        # beyond the local explanation is the server's own reason for not having
        # finished, which for a queued request is the note naming the node that
        # declined and why — without it a wait that ends is indistinguishable from
        # a server that said nothing.
        unfinished: dict[str, Any] | None = None
        with contextlib.suppress(PotluckError):
            unfinished = client.get(f"/v1/requests/{request_id}")
        return _report_ask_failure(directory, exc, status_document=unfinished)
    except PotluckError as exc:
        return _report_ask_failure(directory, exc)
    finally:
        _restore_handlers(handlers)

    document = client.get(f"/v1/requests/{request_id}")
    final_status = str(document.get("status", status)) or status
    cost = _cost_line(client, document, subject_id)
    presenter.finish(
        answer="".join(parts), cost=cost, status=final_status, request_id=request_id
    )
    if final_status in TERMINAL_REQUEST_STATES:
        with contextlib.suppress(OSError):
            pending_path.unlink()
    # Any request that did not succeed, not only a terminal one. A server that
    # holds a dispatch queue leaves a request it could not place waiting rather
    # than ending it, and the reason it is waiting — which node declined, on what
    # local policy — is on that request's note. Printing the note only for FAILED
    # and EXPIRED would swallow exactly the explanation a wait ends without.
    if final_status != "SUCCEEDED":
        _print_billing_note(document)
    if cancelled:
        print(
            f"{PROG}: {ux.describe_request('CANCELLED')} "
            "(asked the server to stop the in-flight work)",
            file=sys.stderr,
        )
        return EXIT_CANCELLED
    return 0 if final_status == "SUCCEEDED" else 1


def _cmd_serve_openai(args: argparse.Namespace) -> int:
    """Run ``serve-openai``: the loopback endpoint until SIGINT/SIGTERM.

    The endpoint raises rather than printing its own refusals, so a non-loopback
    ``--host``, a port that is busy and a config directory with no identity in it
    all reach `main`'s one error path and exit non-zero: a refusal to start is
    never a silent one.
    """
    # Function-local: `localapi` drives this module's consumer seams, so the
    # dependency runs one way and a module-level import would be a cycle.
    from . import localapi

    return localapi.serve_openai(
        settings=localapi.EndpointSettings(
            host=args.host,
            port=args.port,
            max_charge=args.max_charge,
            timeout_s=args.timeout,
            no_node_grace_s=args.no_node_grace,
        ),
        config_dir=_config_dir(args.config_dir),
    )


def _cmd_config_emit(args: argparse.Namespace) -> int:
    """Run ``config emit``: print the configuration a tool needs.

    The block goes to stdout and the guidance to stderr, so a Claude Code block
    can be piped straight into a merge of ``settings.json`` without the guidance
    landing inside the file. Nothing is written and no credential is read: the
    only value emitted into a credential's slot is the endpoint's own documented
    placeholder.

    Raises:
        PotluckError: If ``--host`` is not an address the endpoint would bind, so
            this command cannot emit a configuration that could not work.
    """
    # Function-local: `configemit` renders the addresses of the endpoint
    # `localapi` serves, and `localapi` drives this module's consumer seams, so
    # the dependency runs one way.
    from . import configemit

    target = configemit.target_for(host=args.host, port=args.port, model=args.model)
    if args.claude_code:
        print(canonicalize(configemit.render_claude_code(target)).decode("utf-8"))
        guidance = configemit.claude_code_guidance(target)
    else:
        print(configemit.render_shell(target), end="")
        guidance = configemit.shell_guidance(target)
    for line in guidance:
        print(f"{PROG}: {line}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# doctor: one redacted diagnostic report
# --------------------------------------------------------------------------


def _read_last_error(directory: Path) -> dict[str, Any] | None:
    """Read the last recorded failure code, if there is one."""
    path = directory / _LAST_ERROR_FILE
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        document = canonical.loads_strict(raw)
    except (OSError, PotluckError):
        return None
    return document if isinstance(document, dict) else None


def _node_diagnostics(
    node_path: Path | None, config_document: dict[str, Any] | None,
    key_document: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Summarise the local contribution node, redacted.

    The summary is built through `redacted_node`, the supported renderer: the
    platform token and device private key come back masked and the upstream
    credential never enters it at all. A malformed node file is reported as
    such rather than raised, because a diagnostic that refuses to run when
    something is broken is the least useful kind.
    """
    if node_path is None or not node_path.exists():
        return None
    try:
        node_config, adapter = load_node(node_path)
    except PotluckError as exc:
        return {"present": True, "valid": False, "detail": str(exc)}
    # `redacted_node` is log-safe but not canonical-safe: it carries durations
    # as floats, which canonicalization refuses because a float can silently
    # change meaning between processes. The doctor report must survive
    # `--json`, so the summary is projected onto the fields an operator acts
    # on — identity, policy and the redacted upstream — and the timing knobs
    # are left out of a ticket-sized report.
    summary = redacted_node(node_config, adapter)
    section: dict[str, Any] = {
        "present": True,
        "valid": True,
        "config": {
            "node_id": summary["node_id"],
            "model": summary["model"],
            "adapter_name": summary["adapter_name"],
            "share_enabled": summary["share_enabled"],
            "max_concurrency": summary["max_concurrency"],
            "max_local_reserved_milli_credits": summary[
                "max_local_reserved_milli_credits"
            ],
            "upstream": summary.get("upstream", {}),
        },
        "connected": None,
        "share_enabled": None,
        "last_seen_at": None,
    }
    if config_document is None or key_document is None:
        return section
    try:
        document = signed_client_for(config_document, key_document).get("/v1/nodes")
    except PotluckError:
        return section
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        return section
    section["connected"] = False
    for node in nodes:
        if isinstance(node, dict) and node.get("node_id") == node_config.node_id:
            section["connected"] = True
            section["share_enabled"] = node.get("share_enabled")
            section["last_seen_at"] = node.get("last_seen_at")
            break
    return section


def _collect_diagnostics(
    directory: Path, node_path: Path | None
) -> dict[str, Any]:
    """Assemble the redacted report ``doctor`` prints.

    Every field is chosen so the report can be pasted into a ticket: version,
    a configuration summary, reachability, credential validity, node state,
    unfinished-request count, and the last error code. No field can hold a
    token, a private key, or an upstream key.
    """
    report: dict[str, Any] = {
        "version": _package_version(),
        "protocol_version": PROTOCOL_VERSION,
        "config_dir": str(directory),
        "identity": {"present": False, "detail": "no local identity found"},
        "server": {
            "url": None,
            "reachable": False,
            "detail": "",
            "credential_valid": None,
            "credential_detail": "",
        },
        "node": None,
        "unfinished_requests": 1 if (directory / _PENDING_FILE).exists() else 0,
        "last_error": _read_last_error(directory),
        "ok": False,
    }
    config_document: dict[str, Any] | None = None
    key_document: dict[str, Any] | None = None
    try:
        config_document, key_document = load_joined_identity(directory)
    except PotluckError as exc:
        report["identity"] = {"present": False, "detail": str(exc)}
    else:
        report["identity"] = {
            "present": True,
            **{
                field: config_document.get(field)
                for field in ("server_url", "key_id", "subject_id", "tenant_id", "role")
            },
        }

    if config_document is not None and key_document is not None:
        url = str(config_document.get("server_url", ""))
        report["server"]["url"] = url
        reachable, detail = probe_endpoint(url)
        report["server"]["reachable"] = reachable
        report["server"]["detail"] = detail
        if reachable:
            try:
                signed_client_for(config_document, key_document).get("/v1/checkpoint")
            except PotluckError as exc:
                guidance = ux.classify_failure(
                    error_code=getattr(exc, "error_code", None),
                    http_status=getattr(exc, "http_status", None),
                    message=str(exc),
                )
                report["server"]["credential_valid"] = False
                report["server"]["credential_detail"] = guidance.phrase
                report["server"]["credential_code"] = str(guidance.kind)
                report["server"]["credential_next_step"] = guidance.next_step
            else:
                report["server"]["credential_valid"] = True
                report["server"]["credential_detail"] = "credential valid"

    report["node"] = _node_diagnostics(node_path, config_document, key_document)
    report["ok"] = bool(
        report["identity"]["present"]
        and report["server"]["reachable"]
        and report["server"]["credential_valid"] is not False
    )
    return report


def _print_doctor_report(report: dict[str, Any]) -> None:
    """Print a redacted report as human-readable lines."""
    print(f"{PROG} doctor")
    print(f"  version: {report['version']} (protocol {report['protocol_version']})")
    print(f"  config dir: {report['config_dir']}")
    identity = report["identity"]
    if identity.get("present"):
        print(
            f"  identity: joined (subject={identity.get('subject_id')}, "
            f"tenant={identity.get('tenant_id')}, role={identity.get('role')}, "
            f"key={identity.get('key_id')})"
        )
    else:
        print(f"  identity: not joined ({identity.get('detail')})")
    server = report["server"]
    if server.get("url") is None:
        print("  server: not configured")
    elif server["credential_valid"] is False:
        print(f"  server: {server['url']} reachable, but {server['credential_detail']}")
        # A state without a remedy is just a nicer error: the recovery command
        # belongs on the same screen as the failure it fixes.
        next_step = server.get("credential_next_step")
        if next_step:
            print(f"    {next_step}")
    elif server["credential_valid"] is True:
        print(f"  server: {server['url']} reachable, credential valid")
    else:
        print(f"  server: {server['url']} unreachable ({server['detail']})")
    node = report["node"]
    if node is None:
        print("  node: not configured")
    elif not node.get("valid"):
        print(f"  node: bad configuration ({node.get('detail')})")
    else:
        config = node["config"]
        upstream = config.get("upstream", {})
        state = (
            "connected to the server" if node.get("connected") else "not connected to the server"
        )
        sharing = "sharing enabled" if config.get("share_enabled") else "sharing paused"
        print(
            f"  node: {config.get('node_id')} (model {config.get('model')}, "
            f"upstream {upstream.get('host')}{upstream.get('path') or ''}, "
            f"{state}, {sharing})"
        )
    print(f"  unfinished local requests: {report['unfinished_requests']}")
    last_error = report["last_error"]
    if last_error is None:
        print("  last error: none")
    else:
        print(
            f"  last error: {last_error.get('code')} "
            f"({last_error.get('phrase')}, {last_error.get('at')})"
        )


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Run the ``doctor`` subcommand: one redacted diagnostic report."""
    directory = _config_dir(args.config_dir)
    node_path = args.node_config if args.node_config is not None else directory / _NODE_FILE
    report = _collect_diagnostics(directory, node_path)
    if args.json_output:
        print(canonicalize(report).decode("utf-8"))
    else:
        _print_doctor_report(report)
    return 0 if report["ok"] else 1


# --------------------------------------------------------------------------
# share: the contributor's local control surface
# --------------------------------------------------------------------------


def _node_config_path(args: argparse.Namespace) -> Path:
    """Return the node configuration path the ``share`` subcommands operate on."""
    explicit = getattr(args, "config", None)
    if isinstance(explicit, Path):
        return explicit
    config_dir = getattr(args, "config_dir", None)
    return _config_dir(config_dir if isinstance(config_dir, Path) else None) / _NODE_FILE


def _run_node_forever(node_path: Path) -> int:
    """Load a node config, check its upstream, and hold the server link.

    Shared by ``connect`` and ``share run`` so both paths enforce the same
    pre-dial upstream check: a node whose egress is blocked should learn that
    before the server dispatches work to it and every attempt fails.
    """
    node_config, adapter = load_node(node_path)
    if isinstance(adapter, HttpUpstreamAdapter):
        reachable, detail = probe_endpoint(adapter.endpoint)
        state = "reachable" if reachable else "UNREACHABLE"
        print(f"{PROG}: upstream check: {detail} [{state}]")
        if not reachable:
            guidance = ux.UPSTREAM_UNREACHABLE_GUIDANCE
            print(f"{PROG}: error: {guidance.phrase}; refusing to connect", file=sys.stderr)
            print(f"{PROG}: {guidance.next_step}", file=sys.stderr)
            return 1
    summary = redacted_node(node_config, adapter)
    connector = Connector(node_config, adapter=adapter)
    stop = threading.Event()
    install_stop_handlers(stop)
    try:
        print(
            f"{PROG}: node {node_config.node_id} connecting to "
            f"{node_config.server_url}: {summary}"
        )
        connector.run_forever(stop_event=stop)
    finally:
        connector.stop()
    return 0


def _rewrite_node_config(path: Path, document: dict[str, Any]) -> None:
    """Write a node configuration atomically, at mode 0600.

    The write goes to a sibling temp file and is then renamed over the target. A
    running node polls this file, and a rename is atomic, so the poller can never
    observe a half-written document — it either sees the old one or the new one.
    The temp file is created 0600 because node.json carries the platform token,
    the device key and the upstream credential.
    """
    temp = path.with_name(path.name + ".tmp")
    _write_secret_file(temp, canonicalize(document).decode("utf-8"))
    os.replace(temp, path)


def _set_share_switch(path: Path, *, enabled: bool) -> None:
    """Rewrite ``share_enabled`` in a node configuration, atomically."""
    if not path.exists():
        raise PotluckError(
            f"no node configuration at {path}; run `{PROG} share setup` "
            "(or `potluck join --node`) first"
        )
    raw = read_text_file(path, what="node configuration")
    document = canonical.loads_strict(raw)
    if not isinstance(document, dict):
        raise PotluckError(f"node configuration {path} must be a JSON object")
    document["share_enabled"] = enabled
    _rewrite_node_config(path, document)


def _cmd_share_pause(args: argparse.Namespace) -> int:
    """Run ``share pause``: stop accepting new work without stopping the node."""
    path = _node_config_path(args)
    _set_share_switch(path, enabled=False)
    print(f"{PROG}: sharing paused: wrote share_enabled=false to {path}")
    print(
        f"{PROG}: a running `potluck share run` syncs the pause to the server within one "
        "poll cycle (1 second by default); in-flight work finishes normally"
    )
    return 0


def _cmd_share_resume(args: argparse.Namespace) -> int:
    """Run ``share resume``: accept new work again after a pause."""
    path = _node_config_path(args)
    _set_share_switch(path, enabled=True)
    print(f"{PROG}: sharing resumed: wrote share_enabled=true to {path}")
    print(
        f"{PROG}: a running `potluck share run` takes work from the server again within "
        "one poll cycle (1 second by default)"
    )
    return 0


def _cmd_share_status(args: argparse.Namespace) -> int:
    """Run ``share status``: print the local share picture, redacted."""
    path = _node_config_path(args)
    if not path.exists():
        raise PotluckError(
            f"no node configuration at {path}; run `{PROG} share setup` first"
        )
    node_config, adapter = load_node(path)
    section: dict[str, Any] = {
        "config_path": str(path),
        "node_id": node_config.node_id,
        "model": node_config.model,
        "share_enabled": node_config.share_enabled,
        "max_concurrency": node_config.max_concurrency,
        "max_local_reserved_milli_credits": node_config.max_local_reserved_milli_credits,
        "upstream": (
            adapter.redacted()
            if isinstance(adapter, HttpUpstreamAdapter)
            else {"adapter": adapter.name, "model": adapter.model}
        ),
        "server_url": node_config.server_url,
    }
    if args.json_output:
        print(canonicalize(section).decode("utf-8"))
        return 0
    print(f"{PROG} share status")
    print(f"  node: {section['node_id']} (model {section['model']})")
    print(f"  sharing: {'enabled' if section['share_enabled'] else 'paused'}")
    print(
        f"  local caps: concurrency {section['max_concurrency']}, at most "
        f"{section['max_local_reserved_milli_credits']} milli-credit of in-flight "
        "local exposure"
    )
    upstream = section["upstream"]
    print(
        f"  upstream: {upstream.get('host')}{upstream.get('path') or ''} "
        f"({upstream.get('model')})"
    )
    print(f"  server: {section['server_url']}")
    return 0


def _cmd_share_run(args: argparse.Namespace) -> int:
    """Run ``share run``: start sharing this machine's upstream."""
    return _run_node_forever(_node_config_path(args))


# --------------------------------------------------------------------------
# the contributor wizard (share setup, and `join --node`)
# --------------------------------------------------------------------------


def _read_secret(path: Path | None, *, env: str, what: str) -> str | None:
    """Read a secret from a file then the environment, trimmed.

    Raises:
        PotluckError: If the file exists but cannot be read or is empty.
    """
    if path is not None:
        text = read_text_file(path, what=what).strip()
        if not text:
            raise PotluckError(f"{what} file {path} is empty")
        return text
    value = os.environ.get(env)
    if value:
        return value.strip()
    return None


def _prompt_for(label: str, *, secret: bool) -> str:
    """Prompt for a value on a terminal, or explain a non-interactive fallback.

    Raises:
        PotluckError: When there is no terminal to prompt on.

    A value read with `getpass` is never echoed and never enters the shell's
    history; the non-interactive fallback is a file or an environment variable,
    for the same reason a command-line argument is refused.
    """
    if not sys.stdin.isatty():
        raise PotluckError(
            f"missing {label} and this is not an interactive terminal: supply it through "
            "a file argument or an environment variable (the upstream key through "
            f"--upstream-key-file or {UPSTREAM_KEY_ENV}, the platform token through "
            f"--node-token-file or {NODE_TOKEN_ENV})"
        )
    if secret:
        return getpass.getpass(f"{label}: ").strip()
    return input(f"{label}: ").strip()


def _resolve_node_token(directory: Path, token_file: Path | None) -> str:
    """Return the platform node-link token the wizard should write.

    Sources, in order:

    1. ``--node-token-file`` / ``AIPOTLUCK_NODE_TOKEN``. This is the *override*
       path for an operator who pre-registered a credential in the server's
       ``credentials[]`` by hand (the legacy route the runbook still documents).
       An explicit instruction outranks state on disk, or the override could
       never override anything.
    2. The token ``join --node`` saved in ``node_token.json``. This is the
       invite path, and the reason the wizard no longer asks a contributor for a
       token nobody can hand them.
    3. An interactive prompt — offered only on a terminal, since a prompt nobody
       can answer is just a slow way to fail.

    The token is never printed by any of these paths.

    Raises:
        PotluckError: If none of the sources produced a token and stdin is not a
            terminal. The message names both ways out, including the recovery
            path for a lost token.
    """
    override = _read_secret(token_file, env=NODE_TOKEN_ENV, what="platform node token")
    if override:
        return override
    saved = _load_saved_node_token(directory)
    if saved:
        return saved
    if not sys.stdin.isatty():
        raise PotluckError(
            "missing the platform node token and this is not an interactive terminal: "
            f"run `{PROG} join --node --server <server url> --invite <code>` to have the "
            "server issue and save the token (the invite path needs no hand-copied token), "
            "or supply a token the operator pre-registered with --node-token-file / "
            f"{NODE_TOKEN_ENV}; the recovery path for a lost token is an operator "
            f"`{PROG} admin revoke-key` followed by re-joining on a fresh invite"
        )
    return _prompt_for("platform node token (not echoed)", secret=True)


def _ws_server_url(server_url: str) -> str:
    """Return the node-link WebSocket URL for an http(s) server URL.

    Raises:
        PotluckError: If the URL is not http or https.
    """
    parts = urlsplit(server_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PotluckError(f"server URL must be http or https, not {server_url!r}")
    scheme = "wss" if parts.scheme == "https" else "ws"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{scheme}://{parts.hostname}:{port}/v1/node-link"


def _write_node_config(
    *,
    node_path: Path,
    identity_config: dict[str, Any],
    key_document: dict[str, Any],
    upstream: dict[str, str],
    node_token: str,
) -> dict[str, Any]:
    """Write the local node configuration and return the document written.

    Secrets land in this 0600 file and nowhere else: the upstream key is a field
    of the adapter block, the platform token is a field of the connector block,
    and the device private key is copied from the identity key file. None of
    them is echoed to stdout or stderr by this function or its callers.

    Raises:
        PotluckError: If the joined identity is not a node identity.
    """
    device_id = identity_config.get("device_id")
    device_key_id = identity_config.get("device_key_id")
    provider_id = identity_config.get("provider_id")
    device_ids = (device_id, device_key_id, provider_id)
    if not all(isinstance(value, str) and value for value in device_ids):
        raise PotluckError(
            "this identity has no device binding; join with a node invite "
            f"(`{PROG} join --node --server <url> --invite <code>`) first"
        )
    device_private_key = key_document.get("device_private_key_b64")
    if not isinstance(device_private_key, str) or not device_private_key:
        raise PotluckError(
            "the saved identity has no device private key; re-run "
            f"`{PROG} join --node` to generate one"
        )
    document: dict[str, Any] = {
        "server_url": _ws_server_url(str(identity_config["server_url"])),
        "node_id": str(device_id),
        "device_id": str(device_id),
        "device_key_id": str(device_key_id),
        "provider_id": str(provider_id),
        "database_path": str(node_path.with_name("node.db")),
        "token": node_token,
        "device_private_key_b64": device_private_key,
        "adapter": {
            "kind": "http",
            "endpoint": upstream["endpoint"],
            "api_key": upstream["api_key"],
            "model": upstream["model"],
            "provider_style": upstream["provider_style"],
            "name": upstream["name"],
        },
        # Conservative by default: one concurrent shared call, and a small
        # local exposure ceiling. A new contributor should be able to watch
        # what sharing does before raising either.
        "share_enabled": True,
        "max_concurrency": NODE_DEFAULT_MAX_CONCURRENCY,
        "max_local_reserved_milli_credits": (
            NODE_DEFAULT_MAX_LOCAL_RESERVED_MILLI_CREDITS
        ),
    }
    _write_secret_file(node_path, canonicalize(document).decode("utf-8"))
    return document


def _live_upstream_probe(
    *, endpoint: str, api_key: str, model: str, provider_style: str
) -> tuple[bool, str]:
    """Send one real upstream request to prove the credential works.

    This consumes the operator's own quota, which is why the caller confirms
    first. The result string reports token counts and never the credential.
    """
    adapter = HttpUpstreamAdapter(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        provider_style=provider_style,  # type: ignore[arg-type]
        name="live-probe",
    )
    try:
        result = adapter.complete(
            (user_message("ping"),), request_digest="live-probe", parameters={}
        )
    except PotluckError as exc:
        return False, f"live probe failed: {type(exc).__name__}"
    except Exception as exc:  # adapter errors are plain RuntimeErrors
        return False, f"live probe failed: {type(exc).__name__}"
    usage = result.usage
    tokens = usage.total_tokens if usage is not None else None
    return True, f"live probe succeeded (the upstream returned tokens={tokens})"


def _contributor_self_check(
    *,
    node_path: Path,
    node_config: Any,
    adapter: Any,
    identity_config: dict[str, Any],
    live_probe: bool,
    confirmed: bool,
) -> int:
    """Run the three self-checks and print human conclusions.

    Returns:
        0 when every check passed, 1 otherwise.
    """
    ok = True
    print(f"{PROG}: self-check 1/3 configuration completeness")
    try:
        load_node(node_path)
    except PotluckError as exc:
        ok = False
        print(f"{PROG}:   configuration incomplete: {exc}", file=sys.stderr)
    else:
        print(
            f"{PROG}:   configuration complete (node {node_config.node_id}, "
            f"model {node_config.model}, local caps concurrency "
            f"{node_config.max_concurrency} / "
            f"{node_config.max_local_reserved_milli_credits} milli-credit exposure)"
        )

    print(f"{PROG}: self-check 2/3 upstream reachability")
    if isinstance(adapter, HttpUpstreamAdapter):
        reachable, detail = probe_endpoint(adapter.endpoint)
        if reachable:
            print(f"{PROG}:   upstream reachable: {detail} (TCP only, no credential sent)")
        else:
            ok = False
            guidance = ux.UPSTREAM_UNREACHABLE_GUIDANCE
            print(f"{PROG}:   upstream unreachable: {detail}", file=sys.stderr)
            print(f"{PROG}:   {guidance.next_step}", file=sys.stderr)
        if live_probe and reachable:
            if not confirmed:
                ok = False
                print(
                    f"{PROG}:   a live upstream probe was requested, but it incurs a real "
                    "charge and was not confirmed: add --yes or confirm interactively, "
                    "then retry",
                    file=sys.stderr,
                )
            else:
                probe_ok, probe_detail = _live_upstream_probe(
                    endpoint=adapter.endpoint,
                    api_key=adapter.api_key,
                    model=adapter.model,
                    provider_style=adapter.provider_style,
                )
                if probe_ok:
                    print(f"{PROG}:   {probe_detail}")
                else:
                    ok = False
                    print(f"{PROG}:   {probe_detail}", file=sys.stderr)
    else:
        print(f"{PROG}:   the local upstream is {adapter.name}; no network probe needed")

    print(f"{PROG}: self-check 3/3 server reachability")
    server_url = str(identity_config["server_url"])
    server_reachable, server_detail = probe_endpoint(server_url)
    if server_reachable:
        print(f"{PROG}:   server reachable: {server_detail}")
    else:
        ok = False
        guidance = ux.SERVER_UNREACHABLE_GUIDANCE
        print(f"{PROG}:   server unreachable: {server_detail}", file=sys.stderr)
        print(f"{PROG}:   {guidance.next_step}", file=sys.stderr)
    print(
        f"{PROG}: self-check result: "
        f"{'all passed' if ok else 'problems found; see the conclusions above'}"
    )
    return 0 if ok else 1


def _run_contributor_wizard(
    args: argparse.Namespace, *, directory: Path, identity_config: dict[str, Any],
    key_document: dict[str, Any],
) -> int:
    """Walk a contributor through upstream configuration, self-check, and start.

    Step 1 collects the upstream address/key/model and writes them into
    ``node.json`` (0600). Step 2 checks configuration completeness, upstream
    reachability, and server reachability, and prints a plain conclusion for
    each. Step 3 either starts sharing (``--start``) or prints the exact
    command, because silently turning an onboarding command into a long-running
    foreground process is a surprise, not a convenience.

    The two secrets are read from a file or the environment (or prompted for on
    a terminal) and are never placed on a command line, echoed, or uploaded. The
    platform token additionally has the invite path: :func:`_resolve_node_token`
    falls back to the one ``join --node`` saved, so a contributor who arrived by
    invite is never asked for a credential the server already issued them.
    """
    node_path = directory / _NODE_FILE
    print(f"{PROG}: contributor onboarding wizard (config dir {directory})")

    endpoint = args.upstream_endpoint or _prompt_for(
        "upstream endpoint (full URL, e.g. https://api.example/v1/chat/completions)",
        secret=False,
    )
    model = args.upstream_model or _prompt_for("upstream model", secret=False)
    upstream_key = _read_secret(
        args.upstream_key_file, env=UPSTREAM_KEY_ENV, what="upstream key"
    ) or _prompt_for("upstream key (not echoed)", secret=True)
    node_token = _resolve_node_token(directory, args.node_token_file)

    upstream = {
        "endpoint": endpoint,
        "api_key": upstream_key,
        "model": model,
        "provider_style": args.upstream_style,
        "name": args.upstream_name,
    }
    _write_node_config(
        node_path=node_path,
        identity_config=identity_config,
        key_document=key_document,
        upstream=upstream,
        node_token=node_token,
    )
    node_config, adapter = load_node(node_path)
    print(
        f"{PROG}: [step 1] wrote the local configuration {node_path} (0600): "
        f"upstream {endpoint} / model {model}; the key was not uploaded and not printed"
    )

    confirmed = bool(args.yes)
    if args.probe_upstream_live and not confirmed:
        confirmed = _confirm_live_probe()
    code = _contributor_self_check(
        node_path=node_path,
        node_config=node_config,
        adapter=adapter,
        identity_config=identity_config,
        live_probe=bool(args.probe_upstream_live),
        confirmed=confirmed,
    )
    if code != 0:
        return code
    if args.start:
        print(f"{PROG}: [step 3] starting to share")
        return _run_node_forever(node_path)
    print(
        f"{PROG}: [step 3] ready; run `{PROG} share run --config {node_path}` to start "
        f"sharing, `{PROG} share pause` to pause and `{PROG} share resume` to resume"
    )
    return 0


def _confirm_live_probe() -> bool:
    """Ask the operator to allow a billable upstream probe.

    Returns:
        True when the operator agreed. A non-interactive caller is refused
        rather than assumed to consent: this action spends real money.
    """
    if not sys.stdin.isatty():
        return False
    answer = input(
        "a live probe sends one real request to the upstream and incurs a charge; "
        "continue? [y/N] "
    ).strip().lower()
    return answer in ("y", "yes")


def _cmd_share_setup(args: argparse.Namespace) -> int:
    """Run ``share setup``: the contributor wizard, standalone."""
    directory = _config_dir(args.config_dir)
    identity_config, key_document = load_joined_identity(directory)
    return _run_contributor_wizard(
        args, directory=directory, identity_config=identity_config, key_document=key_document
    )


def _imported_source(args: argparse.Namespace) -> configimport.PartialUpstream:
    """Read the one configuration source this invocation named.

    Args:
        args: The parsed ``contribute import-config`` arguments.

    Returns:
        What that source yielded, including the fields it did not name.

    Raises:
        ConfigImportError: If the source cannot be read or is malformed.
    """
    if args.from_env:
        return configimport.read_environment(os.environ)
    if args.from_claude_code is not None:
        return configimport.read_claude_code(Path(str(args.from_claude_code)))
    return configimport.read_codex_config(
        Path(str(args.from_codex)), os.environ
    )


def _cmd_contribute_import_config(args: argparse.Namespace) -> int:
    """Run ``contribute import-config``: write an imported upstream into node.json.

    Two cases, because they owe the operator different things. An existing
    node.json keeps everything that belongs to this node — the platform token, the
    device binding, the local limits, the share switch — and only its adapter
    block is replaced. With no node.json yet, one is created from the identity
    ``join --node`` saved, which is the only source of the device key and the
    platform token this command will not invent.

    The write is atomic and 0600 either way, because a running node polls the file
    and because it now holds an upstream credential. Nothing printed here names
    the key: the summary is :meth:`aipotluck.configimport.Upstream.summary`, which
    cannot render one.
    """
    directory = _config_dir(args.config_dir)
    node_path = _node_config_path(args)
    upstream = configimport.resolve_upstream(
        _imported_source(args),
        endpoint=args.upstream_endpoint,
        api_key=_read_secret(
            args.upstream_key_file, env=UPSTREAM_KEY_ENV, what="upstream key"
        ),
        model=args.upstream_model,
        name=args.upstream_name,
    )
    if node_path.exists():
        merged = configimport.merge_adapter(load_document(node_path), upstream)
        _rewrite_node_config(node_path, merged)
        wrote = (
            f"replaced the adapter block of {node_path} and left the rest of the "
            "node configuration as it was"
        )
    else:
        identity_config, key_document = load_joined_identity(directory)
        _write_node_config(
            node_path=node_path,
            identity_config=identity_config,
            key_document=key_document,
            upstream=upstream.as_mapping(),
            node_token=_resolve_node_token(directory, args.node_token_file),
        )
        wrote = f"wrote {node_path} from the node identity in {directory}"
    print(f"{PROG}: imported {upstream.summary()}")
    print(
        f"{PROG}: {wrote}, at mode 0600; the upstream key was not printed and was "
        "not uploaded"
    )
    print(
        f"{PROG}: check it with `{PROG} share status --config {node_path}`, then "
        f"start sharing with `{PROG} share run --config {node_path}`"
    )
    return 0


# --------------------------------------------------------------------------
# reconciliation: verify one receipt, and diff each side against the other
# --------------------------------------------------------------------------


#: How each verification source is named to an operator. The source is part of
#: the verdict: the operator's own store holds the signed usage report, so all
#: five dimensions are assessable, while the HTTP path holds three documents and
#: `metering_provenance` stays `unknown`.
_SOURCE_LABELS = {
    "server-store": "the server's own store",
    "server-http": "server HTTP",
}


def print_verification(verification: ReceiptVerification) -> None:
    """Print one receipt's five-dimension report in the renderer's own layout.

    The dimensions come from `VerificationReport.render` verbatim, so this
    command and any other caller of the library agree on one spelling of the
    five verdicts. What is added here is the part a library cannot know: which
    verdicts failed, which stayed ``unknown`` for want of evidence, and the
    standing caveat that a signed usage report proves who reported the numbers.
    """
    source = _SOURCE_LABELS[verification.source]
    print(f"{PROG} verify receipt {verification.receipt_id} (source: {source})")
    print(verification.report.render())
    for finding in verification.findings:
        print(f"  ! {finding}")
    verdicts = verification.verdicts()
    failed = [name for name in DIMENSIONS if verdicts[name] == "fail"]
    unknown = [name for name in DIMENSIONS if verdicts[name] == "unknown"]
    if failed:
        print(
            f"{PROG}: failed verdicts: {', '.join(failed)} -- the entries marked ! "
            "above name the records"
        )
    if unknown:
        print(
            f"{PROG}: undetermined: {', '.join(unknown)} "
            "(missing evidence, which is not a pass)"
        )
    if not failed and not unknown:
        print(f"{PROG}: all five verdicts passed")
    if verdicts["metering_provenance"] != "fail":
        print(
            f"{PROG}: note: all_pass only summarizes the five verdicts, it is not a "
            "sixth one; metering_provenance is the evidence level -- node_reported "
            "proves who reported the numbers, not that the upstream really consumed "
            "those tokens"
        )


def _verify_receipt_as_consumer(directory: Path, receipt_id: str) -> ReceiptVerification:
    """Verify one receipt from the three documents a consumer can fetch.

    All three reads go through the `SignedClient` the joined identity already
    authenticates with, so this command needs no token and prints none. The
    account history endpoint takes no account id: the server derives the account
    from the credential, which is what stops this command reading anybody else's
    books even by accident.

    Raises:
        PotluckError: If no identity is saved here, the server refuses a read, or
            an answer cannot be parsed.
    """
    config_document, key_document = load_joined_identity(directory)
    client = signed_client_for(config_document, key_document)
    receipt_document = client.get(f"/v1/receipts/{quote(receipt_id, safe='')}")
    return verify_receipt_from_server(
        receipt_document=receipt_document,
        checkpoint_document=client.get("/v1/checkpoint"),
        history_document=client.get("/v1/account/history"),
    )


def _cmd_verify_receipt(args: argparse.Namespace) -> int:
    """Run ``verify receipt``: the five-dimension check over one receipt.

    The consumer view reads over HTTP under the joined identity, so
    ``--config-dir`` is required; the operator view that opens a server's own
    store is ``potluck-server verify receipt``.

    Returns:
        0 when no dimension contradicted the record, 1 when one did. ``unknown``
        is not a contradiction: a dimension reports it when the verifier holds no
        evidence either way, and failing on it would make an honest consumer run
        — which never holds a signed usage report — look broken.
    """
    directory = args.config_dir
    if directory is None:
        print(
            f"{PROG}: error: --config-dir is required; the consumer view reads over "
            "HTTP as the joined identity (the operator view that reads a server's "
            "own store is `potluck-server verify receipt --config`)",
            file=sys.stderr,
        )
        return 2
    verification = _verify_receipt_as_consumer(_config_dir(directory), args.receipt_id)
    if args.json_output:
        print(canonicalize(verification.to_payload()).decode("utf-8"))
    else:
        print_verification(verification)
    return 1 if verification.failed else 0


def print_differences(
    *,
    title: str,
    differences: Sequence[Difference],
    left: str,
    right: str,
) -> None:
    """Print a difference table: one row per record, naming the record.

    Columns are separated rather than padded to a fixed width, because a
    difference's whole value is the detail in it — a truncated hash or a cut-off
    account name sends the operator back to the database this command just read.
    """
    print(title)
    if not differences:
        print("  no differences: both sides agree")
        return
    print(f"  {len(differences)} difference(s):")
    print(f"  record | {left} | {right} | difference kind")
    for item in differences:
        print(f"  {item.subject} | {item.local} | {item.remote} | {item.kind}")


def _http_server_url(server_url: str) -> str:
    """Return the HTTP base URL of the server a node configuration's URL names.

    ``node.json`` stores the WebSocket URL the connector dials, which is the
    inverse of `_ws_server_url`. Reconciliation reads a plain HTTP endpoint with
    the same credential, so the scheme is mapped back and the path dropped:
    carrying ``/v1/node-link`` into an HTTP GET would ask the gateway to upgrade
    a connection that is not a WebSocket.

    Raises:
        PotluckError: If the URL is not an absolute ``ws(s)://`` URL.
    """
    parts = urlsplit(server_url)
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise PotluckError(f"node server URL must be ws or wss, not {server_url!r}")
    scheme = "https" if parts.scheme == "wss" else "http"
    port = parts.port or (443 if parts.scheme == "wss" else "80")
    return f"{scheme}://{parts.hostname}:{port}"


def _cmd_share_reconcile(args: argparse.Namespace) -> int:
    """Run ``share reconcile``: this node's record against the server's claim.

    The local half is read straight out of ``node.db`` and the remote half from
    one read-only endpoint scoped to this node's own credential, so the command
    sees exactly what the contributor and the server each hold about the same
    attempts. Neither side is written to, and neither database is created: a
    ``node.db`` that is missing is reported, since building an empty one would
    both hide the loss and hand `share run` a node with no history.

    Raises:
        PotluckError: If this machine has no node configuration, or the one it
            has names a database that does not exist.

    Returns:
        0 when the two sides agree, 1 when any difference stands.
    """
    path = _node_config_path(args)
    if not path.exists():
        raise PotluckError(f"no node configuration at {path}; run `{PROG} share setup` first")
    node_config, _ = load_node(path)
    # The node's own database must be there: only `share run` creates one. Were
    # it created here instead, the reconcile would report every server attempt as
    # missing locally and leave behind an empty node.db that a later `share run`
    # would continue with as if this node had never executed anything.
    store = NodeStore.open_existing(node_config.database_path)
    try:
        executions = store.executions()
        ack_cursors = store.ack_cursors()
        spool = store.spooled()
    finally:
        store.close()
    # The token travels only in the Authorization header; `bearer_get` scrubs it
    # from every message it raises, so a refused read cannot print it.
    document = bearer_get(
        _http_server_url(node_config.server_url), node_config.token, "/v1/node/attempts"
    )
    answered = document.get("node_ids")
    known = {
        str(item) for item in answered if isinstance(item, str)
    } if isinstance(answered, list) else set()
    remote = [
        NodeAttemptView.from_payload(entry)
        for entry in document.get("attempts", [])
        if isinstance(entry, dict)
    ]
    differences = reconcile_node(
        node_id=node_config.node_id,
        executions=executions,
        ack_cursors=ack_cursors,
        spool=spool,
        remote_attempts=remote,
    )
    if node_config.node_id not in known:
        # Not a difference between two records of one attempt, but the reason
        # every attempt below is missing: the server does not know this node, so
        # it listed nothing to compare against. Saying so first keeps "the server
        # forgot my work" distinguishable from "I am pointing at the wrong server".
        differences.insert(
            0,
            Difference(
                subject=f"node {node_config.node_id}",
                kind="server_does_not_know_this_node",
                local=f"this machine dials as {node_config.node_id}",
                remote=f"the server listed {sorted(known) or '<no node>'}",
            ),
        )
    if args.json_output:
        print(
            canonicalize(
                {
                    "node_id": node_config.node_id,
                    "server_node_ids": sorted(known),
                    "executions": len(executions),
                    "remote_attempts": len(remote),
                    "differences": [item.to_payload() for item in differences],
                }
            ).decode("utf-8")
        )
    else:
        print_differences(
            title=(
                f"{PROG} share reconcile (node {node_config.node_id}, "
                f"{len(executions)} local execution records, {len(remote)} server attempts)"
            ),
            differences=differences,
            left="local record",
            right="server record",
        )
    return 1 if differences else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``potluck`` command-line interface.

    Args:
        argv: Argument list to parse, excluding the program name. Defaults to
            ``sys.argv[1:]`` when ``None``.

    Returns:
        The process exit code: 0 on success, 1 on any handled failure.
        Argument-parsing errors exit with argparse's own status 2.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except PotluckError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""AI potluck: bring your own compute, share the meal.

The consumer and contributor client of the potluck network, and the protocol
and verification core both sides of the network speak: canonical encoding,
identity, signed records, the wire schema, metering, the ledger, the
transparency log and offline settlement verification all live here, so a
client can verify a receipt without trusting the server. Server bookkeeping
and the gateway live in the ``ai-potluck-server`` package, which depends on
this one and imports these verification primitives to keep its own books.
"""

from __future__ import annotations

__version__ = "0.3.0"

"""Client TLS defaults, honoring a privately deployed CA.

Deployment modes that terminate TLS at a reverse proxy typically present a
certificate from a private CA (self-signed for an IP-hosted server, or an
internal CA). The system trust store knows nothing about that CA, so a client
on a machine that did not install it globally fails verification with
``unable to get local issuer certificate``.

``client_ssl_context`` is the single place every outbound TLS client (the HTTP
upstream adapter and the node-link WebSocket) builds its context: when
``AIPOTLUCK_CA_FILE`` points at a PEM CA bundle, that bundle is what the
connection verifies against; otherwise the system default applies unchanged.
"""

import os
import ssl

CA_FILE_ENV = "AIPOTLUCK_CA_FILE"


def client_ssl_context() -> ssl.SSLContext:
    """Return the default client context, honoring ``AIPOTLUCK_CA_FILE``."""
    ca_file = os.environ.get(CA_FILE_ENV)
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    return ssl.create_default_context()

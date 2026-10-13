"""The node and consumer configuration loaders: refusals and secret hygiene.

Every test drives the real operators' path: a JSON file on disk is loaded and
the constructed objects are asserted on concretely. The refusal tests pin the
fail-closed behaviour the network contract promises: a node without a usable
upstream never connects, and a fake upstream never silently swallows an
api_key. Every failure path is also checked for secret leakage: the token
present in the config must not appear in any raised text. The server-side
loader (``load_server``) lives in the ``ai-potluck-server`` repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aipotluck.adapters import FakeUpstreamAdapter, HttpUpstreamAdapter
from aipotluck.deploy import (
    DeploymentError,
    load_consumer,
    load_node,
    redacted_node,
)
from aipotluck.identity import generate_key_pair

CONSUMER_TOKEN = "consumer-token-deploy-secret"
NODE_TOKEN = "node-token-deploy-secret"
API_KEY = "sk-upstream-key-deploy-secret"


def _write_json(path: Path, document: dict[str, Any]) -> Path:
    """Write a strict-JSON configuration file."""
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _node_document(tmp_path: Path, **adapter: Any) -> dict[str, Any]:
    """Build a valid node.json body; adapter fields can be overridden."""
    _device_public, device_private = generate_key_pair()
    http_adapter: dict[str, Any] = {
        "kind": "http",
        "endpoint": "https://api.example.com/v1/messages",
        "api_key": API_KEY,
        "model": "coding-pool",
        "provider_style": "anthropic",
        "name": "main",
    }
    http_adapter.update(adapter)
    return {
        "server_url": "ws://127.0.0.1:9100/v1/node-link",
        "node_id": "node-A",
        "device_id": "device-A",
        "device_key_id": "device-key-A",
        "provider_id": "provider-A",
        "database_path": str(tmp_path / "node.db"),
        "token": NODE_TOKEN,
        "adapter": http_adapter,
        "device_private_key_b64": device_private,
        "share_enabled": True,
        "max_concurrency": 2,
        "max_local_reserved_milli_credits": 15_000,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        # Missing endpoint on an http adapter.
        {"kind": "http", "endpoint": None},
        # A relative endpoint is not an absolute URL.
        {"kind": "http", "endpoint": "/v1/messages"},
        # An http adapter without a credential has nothing to authenticate with.
        {"kind": "http", "api_key": None},
        # A fake upstream must never silently accept a credential.
        {"kind": "fake", "endpoint": None, "api_key": API_KEY},
    ],
)
def test_load_node_refuses_unusable_adapters(tmp_path: Path, mutation: dict[str, Any]) -> None:
    document = _node_document(tmp_path, **mutation)
    path = _write_json(tmp_path / "node.json", document)
    with pytest.raises(DeploymentError) as excinfo:
        load_node(path)
    message = str(excinfo.value)
    assert API_KEY not in message
    assert NODE_TOKEN not in message


def test_load_node_builds_connector_config_and_http_adapter(tmp_path: Path) -> None:
    """The adapter is the only carrier of the api_key; the config has none."""
    path = _write_json(tmp_path / "node.json", _node_document(tmp_path))
    config, adapter = load_node(path)
    assert isinstance(adapter, HttpUpstreamAdapter)
    assert adapter.endpoint == "https://api.example.com/v1/messages"
    assert adapter.api_key == API_KEY
    assert adapter.model == "coding-pool"
    assert adapter.provider_style == "anthropic"
    assert adapter.name == "main"

    assert config.node_id == "node-A"
    assert config.device_id == "device-A"
    assert config.device_key_id == "device-key-A"
    assert config.provider_id == "provider-A"
    assert config.server_url == "ws://127.0.0.1:9100/v1/node-link"
    assert config.token == NODE_TOKEN
    assert config.adapter_name == "main"
    assert config.model == "coding-pool"
    assert config.share_enabled is True
    assert config.max_concurrency == 2
    assert config.max_local_reserved_milli_credits == 15_000
    assert not hasattr(config, "api_key")
    assert not hasattr(config, "endpoint")

    summary = json.dumps(redacted_node(config, adapter))
    assert API_KEY not in summary
    assert NODE_TOKEN not in summary
    assert "api.example.com" in summary, "the redacted view names the upstream host"


def test_load_node_builds_fake_adapter(tmp_path: Path) -> None:
    document = _node_document(
        tmp_path, kind="fake", endpoint=None, api_key=None, provider_style="openai"
    )
    path = _write_json(tmp_path / "node.json", document)
    config, adapter = load_node(path)
    assert isinstance(adapter, FakeUpstreamAdapter)
    assert adapter.name == "main"
    assert adapter.model == "coding-pool"
    assert config.adapter_name == "main"


def test_load_consumer_round_trips_and_refuses_non_http(tmp_path: Path) -> None:
    document = {"server_url": "https://server.example.com", "token": CONSUMER_TOKEN}
    path = _write_json(tmp_path / "consumer.json", document)
    assert load_consumer(path) == document

    document["server_url"] = "ftp://server.example.com"
    path = _write_json(tmp_path / "bad-scheme.json", document)
    with pytest.raises(DeploymentError) as excinfo:
        load_consumer(path)
    assert "https://" in str(excinfo.value)
    assert CONSUMER_TOKEN not in str(excinfo.value)

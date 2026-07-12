"""Sandbox worker: how the run's secrets are resolved from the stdin request.

Container mode delivers secrets out-of-band via a mounted file referenced by
``secrets_path``; subprocess mode still sends them in-band as ``secrets``. The
worker must prefer the file when present and fall back to in-band otherwise.
"""
import json

import pytest

from catlico_plugin_sdk._worker import _load_secrets


def test_reads_secrets_from_secrets_path_file(tmp_path):
    secrets = {"api_key": "from-the-mounted-file", "token": "abc123"}
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(secrets))
    # secrets_path present -> file wins, even if in-band `secrets` also present.
    request = {"secrets_path": str(path), "secrets": {"api_key": "stale-inband"}}
    assert _load_secrets(request) == secrets


def test_falls_back_to_inband_secrets_when_no_path():
    request = {"secrets": {"api_key": "trusted-subprocess-value"}}
    assert _load_secrets(request) == {"api_key": "trusted-subprocess-value"}


def test_empty_when_neither_present():
    assert _load_secrets({}) == {}


def test_missing_secrets_path_raises_clearly(tmp_path):
    # secrets_path set but the file is absent: fail loudly rather than silently
    # running the plugin with no secrets.
    request = {"secrets_path": str(tmp_path / "does-not-exist.json")}
    with pytest.raises(FileNotFoundError):
        _load_secrets(request)

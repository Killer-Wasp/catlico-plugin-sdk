"""The plugin worker: secret resolution, app loading, trigger validation, actions.

The worker runs the plugin's ``Catlico`` app in a child process. These tests
drive ``_run`` directly (in-process) against a plugin module written to a temp
dir and put on ``plugin_path`` — the same wiring the runner uses, minus the
subprocess boundary.
"""
import json
import sys
import textwrap
from pathlib import Path

import pytest

from catlico_plugin_sdk._worker import _load_secrets, _run


# --- secret resolution -------------------------------------------------------


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
    request = {"secrets_path": str(tmp_path / "does-not-exist.json")}
    with pytest.raises(FileNotFoundError):
        _load_secrets(request)


# --- app loading + dispatch --------------------------------------------------

_APP_SOURCE = """
from catlico_plugin_sdk import Catlico
from catlico_plugin_sdk.plugin import ConfigError

catlico = Catlico()

@catlico.event("observable.created")
async def handle(event, ctx):
    print("plugin ran")

@catlico.health()
async def health(ctx):
    if ctx.config.get("break_health"):
        raise ConfigError("health is unhappy")
    return {"ok": True, "checked": True}
"""


def _write_app(tmp_path: Path, name: str, source: str) -> str:
    """Write a plugin entrypoint module named ``name`` and return its dir."""
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(source))
    return str(tmp_path)


def _request(plugin_path: str, module: str, **overrides) -> dict:
    base = {
        "run_id": "run-1",
        "plugin_module": module,
        "plugin_object": "catlico",
        "plugin_path": plugin_path,
        "declared_triggers": ["observable.created"],
        "event": {
            "event_id": "audit:1",
            "event_type": "observable.created",
            "organisation_id": "org-a",
            "object": {"type": "observable", "id": "obs-1"},
            "data": {"observable_type": "ip", "data": "1.2.3.4"},
        },
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clean_modules():
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


async def test_dispatch_success(tmp_path):
    path = _write_app(tmp_path, "wk_ok", _APP_SOURCE)
    result = await _run(_request(path, "wk_ok"))
    assert result["status"] == "success"
    assert result["run_id"] == "run-1"


async def test_trigger_mismatch_is_config_failure(tmp_path):
    path = _write_app(tmp_path, "wk_mismatch", _APP_SOURCE)
    request = _request(
        path, "wk_mismatch", declared_triggers=["observable.created", "case.created"]
    )
    result = await _run(request)
    assert result["status"] == "failure"
    assert result["error_kind"] == "config"
    assert "case.created" in result["error"]


async def test_entrypoint_not_a_catlico_app_is_config_failure(tmp_path):
    path = _write_app(tmp_path, "wk_notapp", "catlico = object()\n")
    result = await _run(_request(path, "wk_notapp"))
    assert result["status"] == "failure"
    assert result["error_kind"] == "config"
    assert "Catlico" in result["error"]


async def test_health_action_returns_health_dict(tmp_path):
    path = _write_app(tmp_path, "wk_health", _APP_SOURCE)
    result = await _run(_request(path, "wk_health", action="health"))
    assert result["status"] == "success"
    assert result["health"] == {"ok": True, "checked": True}


async def test_health_action_classifies_failure(tmp_path):
    path = _write_app(tmp_path, "wk_health_fail", _APP_SOURCE)
    request = _request(
        path, "wk_health_fail", action="health", config={"break_health": True}
    )
    result = await _run(request)
    assert result["status"] == "failure"
    assert result["error_kind"] == "config"

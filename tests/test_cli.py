"""Tests for the ``catlico-plugin`` CLI and the manifest schema it validates."""
import io
import json
from pathlib import Path

import pytest

from catlico_plugin_sdk.cli import main, run_command, validate_command
from catlico_plugin_sdk.manifest import validate_manifest

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "demo_plugin"


# --- manifest validation ---


def test_valid_manifest_has_no_errors():
    manifest = {
        "id": "x",
        "version": "1.0.0",
        "entrypoint": "x.plugin:X",
        "triggers": ["observable.created"],
        "permissions": ["read:observable"],
        "timeout_seconds": 30,
        "configuration": [{"name": "key", "type": "string"}],
    }
    assert validate_manifest(manifest) == []


def test_missing_required_fields_reported():
    errors = validate_manifest({})
    assert any("id" in e for e in errors)
    assert any("version" in e for e in errors)
    assert any("entrypoint" in e for e in errors)
    assert any("trigger" in e for e in errors)


def test_unknown_permission_reported():
    errors = validate_manifest(
        {
            "id": "x",
            "version": "1",
            "entrypoint": "a:B",
            "triggers": ["t"],
            "permissions": ["read:observable", "delete:everything"],
        }
    )
    assert any("delete:everything" in e for e in errors)


def test_bad_config_parameter_reported():
    errors = validate_manifest(
        {
            "id": "x",
            "version": "1",
            "entrypoint": "a:B",
            "triggers": ["t"],
            "configuration": [
                {"type": "string"},  # missing name
                {"name": "k", "type": "mystery"},  # bad type
                {"name": "k", "type": "string"},  # duplicate name
            ],
        }
    )
    assert any("missing required field: name" in e for e in errors)
    assert any("unknown type" in e for e in errors)
    assert any("duplicate" in e for e in errors)


# --- validate command ---


def test_validate_command_ok_on_fixture():
    out = io.StringIO()
    code = validate_command(str(FIXTURE), out=out)
    assert code == 0
    assert "valid" in out.getvalue()


def test_validate_command_reports_errors(tmp_path):
    (tmp_path / "catlico-plugin.toml").write_text('id = "broken"\n')
    out = io.StringIO()
    code = validate_command(str(tmp_path), out=out)
    assert code == 1
    assert "error" in out.getvalue()


def test_validate_command_missing_manifest(tmp_path):
    out = io.StringIO()
    assert validate_command(str(tmp_path), out=out) == 1
    assert "no catlico-plugin.toml" in out.getvalue()


# --- run command ---


def _write_event(tmp_path: Path, data: dict) -> str:
    envelope = {
        "event_id": "audit:1",
        "event_type": "observable.created",
        "organisation_id": "org-a",
        "object": {"type": "observable", "id": "obs-1"},
        "data": data,
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(envelope))
    return str(path)


def test_run_command_success(tmp_path):
    event = _write_event(tmp_path, {"observable_type": "ip", "data": "1.2.3.4"})
    out = io.StringIO()
    code = run_command(str(FIXTURE), event, out=out)
    text = out.getvalue()
    assert code == 0
    assert "status: success" in text
    assert "results (1)" in text
    assert "progress (1)" in text
    # default config from the manifest ("hello") flows into the enrichment.
    assert "hello 1.2.3.4" in text


def test_run_command_config_override(tmp_path):
    event = _write_event(tmp_path, {"observable_type": "ip", "data": "9.9.9.9"})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"greeting": "howdy"}))
    out = io.StringIO()
    code = run_command(str(FIXTURE), event, config_path=str(config), out=out)
    assert code == 0
    assert "howdy 9.9.9.9" in out.getvalue()


def test_run_command_skips_non_ip(tmp_path):
    event = _write_event(tmp_path, {"observable_type": "domain", "data": "x.com"})
    out = io.StringIO()
    code = run_command(str(FIXTURE), event, out=out)
    assert code == 0
    assert "status: skipped" in out.getvalue()


def test_run_command_reports_plugin_failure(tmp_path):
    event = _write_event(tmp_path, {"observable_type": "ip", "data": ""})
    out = io.StringIO()
    code = run_command(str(FIXTURE), event, out=out)
    text = out.getvalue()
    assert code == 1
    assert "status: failure" in text
    assert "error_kind: input" in text


# --- argv entrypoint ---


def test_main_validate_returns_zero():
    assert main(["validate", str(FIXTURE)]) == 0


def test_main_run_requires_event():
    with pytest.raises(SystemExit):
        main(["run", str(FIXTURE)])

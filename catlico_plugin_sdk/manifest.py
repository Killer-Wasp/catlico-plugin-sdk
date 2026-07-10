"""Plugin manifest schema and validation.

The manifest (``catlico-plugin.toml``) is a plugin's declaration of identity,
capabilities, event triggers, permissions, and configurable parameters. This
module is the SDK-side schema authority used by the ``catlico-plugin validate``
CLI and by the test kit (``catlico_plugin_sdk.testing``).

The permission vocabulary here mirrors what the Catlico runtime actually enforces
(``catlico-api`` ``app/api/internal/routes/plugin_runtime.py`` and the runner's
install validator) — a plugin can only ever be granted permissions from this set,
so a manifest that requests anything else is rejected before it ships.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

MANIFEST_FILENAME = "catlico-plugin.toml"

#: Permissions a plugin may declare. Kept in lockstep with the Catlico runtime's
#: enforced set (``plugin_runtime`` ``_require``/``_require_any``) and the runner
#: installer's ``_ALLOWED_PERMISSIONS``.
PERMISSIONS: frozenset[str] = frozenset(
    {
        "read:case",
        "read:alert",
        "read:observable",
        "write:case",
        "write:task",
        "write:observable",
        "write:observable_enrichment",
        "write:plugin_result",
    }
)

#: Config parameter ``type`` values the schema recognises.
PARAM_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "float", "boolean", "array", "cron"}
)


def load_manifest(path: str | Path) -> dict:
    """Load a manifest from a plugin directory or a manifest file path."""
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST_FILENAME
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def validate_manifest(manifest: dict) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid).

    Checks required top-level fields, the ``module:Class`` entrypoint shape, that
    at least one trigger is declared, that requested permissions are in the
    enforced vocabulary, a positive integer timeout, and every
    ``[[configuration]]`` parameter declaration.
    """
    errors: list[str] = []
    for field in ("id", "version", "entrypoint"):
        if not manifest.get(field):
            errors.append(f"missing required field: {field}")

    entrypoint = manifest.get("entrypoint", "")
    if entrypoint and ":" not in entrypoint:
        errors.append("entrypoint must be 'module:Class'")

    if not manifest.get("triggers"):
        errors.append("must declare at least one trigger")

    bad_perms = sorted(set(manifest.get("permissions", [])) - PERMISSIONS)
    if bad_perms:
        errors.append(f"unknown permissions: {bad_perms}")

    timeout = manifest.get("timeout_seconds", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        errors.append("timeout_seconds must be a positive integer")

    errors.extend(_validate_configuration(manifest.get("configuration", [])))
    return errors


def _validate_configuration(configuration: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(configuration, list):
        return ["configuration must be an array of parameter tables"]

    seen: set[str] = set()
    for index, param in enumerate(configuration):
        where = f"configuration[{index}]"
        if not isinstance(param, dict):
            errors.append(f"{where} must be a table")
            continue

        name = param.get("name")
        if not name:
            errors.append(f"{where} missing required field: name")
        else:
            where = f"configuration '{name}'"
            if name in seen:
                errors.append(f"duplicate configuration parameter: {name}")
            seen.add(name)

        ptype = param.get("type")
        if not ptype:
            errors.append(f"{where} missing required field: type")
        elif ptype not in PARAM_TYPES:
            errors.append(
                f"{where} has unknown type '{ptype}' "
                f"(allowed: {sorted(PARAM_TYPES)})"
            )

        choices = param.get("choices")
        if choices is not None and not isinstance(choices, list):
            errors.append(f"{where} choices must be an array")

        for bound in ("min", "max"):
            if bound in param:
                value = param[bound]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"{where} {bound} must be a number")

    return errors


def manifest_defaults(manifest: dict) -> dict:
    """Non-secret config defaults declared by the manifest.

    Mirrors the server's precedence keys: a parameter's ``defaultValue`` (the
    convention used in ``catlico-plugin.toml``) or ``default``. Secret parameters
    never carry a default value into config.
    """
    defaults: dict = {}
    for param in manifest.get("configuration", []) or []:
        if not isinstance(param, dict) or not param.get("name"):
            continue
        if param.get("secret") or param.get("type") == "secret":
            continue
        if "defaultValue" in param:
            defaults[param["name"]] = param["defaultValue"]
        elif "default" in param:
            defaults[param["name"]] = param["default"]
    return defaults

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

#: Config parameter ``type`` values the schema recognises. Includes ``secret``
#: (an alternative to the ``secret = true`` boolean — the Catlico API's
#: ``_is_secret_param`` accepts both). This is *not* a closed vocabulary:
#: neither the Catlico API nor the runner installer rejects an unrecognised
#: ``type``, so the validator only *warns* about types outside this set (see
#: ``manifest_warnings``) rather than failing a manifest the platform accepts.
PARAM_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "float", "boolean", "array", "cron", "secret"}
)


def load_manifest(path: str | Path) -> dict:
    """Load a manifest from a plugin directory or a manifest file path."""
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST_FILENAME
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def validate_manifest(manifest: dict) -> list[str]:
    """Return the manifest's hard validation errors (empty == valid to ship).

    Hard errors are only for shapes the Catlico platform rejects: missing
    required top-level fields, a malformed ``module:Class`` entrypoint, no
    triggers, a permission outside the enforced vocabulary, a non-positive
    timeout, and structurally malformed ``[[configuration]]`` declarations.
    Advisory issues (e.g. an unrecognised config ``type``, which production
    treats as freeform) are returned by ``manifest_warnings`` and must not fail a
    manifest the platform would accept.
    """
    return _check_manifest(manifest)[0]


def manifest_warnings(manifest: dict) -> list[str]:
    """Return non-fatal advisory issues (empty == none). See ``validate_manifest``."""
    return _check_manifest(manifest)[1]


def _check_manifest(manifest: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for field in ("id", "version", "entrypoint"):
        if not manifest.get(field):
            errors.append(f"missing required field: {field}")

    entrypoint = manifest.get("entrypoint", "")
    if entrypoint and ":" not in entrypoint:
        errors.append("entrypoint must be 'module:app_object' (e.g. 'main:catlico')")

    if not manifest.get("triggers"):
        errors.append("must declare at least one trigger")

    bad_perms = sorted(set(manifest.get("permissions", [])) - PERMISSIONS)
    if bad_perms:
        errors.append(f"unknown permissions: {bad_perms}")

    timeout = manifest.get("timeout_seconds", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        errors.append("timeout_seconds must be a positive integer")

    _check_configuration(manifest.get("configuration", []), errors, warnings)
    return errors, warnings


def _check_configuration(
    configuration: object, errors: list[str], warnings: list[str]
) -> None:
    if not isinstance(configuration, list):
        errors.append("configuration must be an array of parameter tables")
        return

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
            # Production treats ``type`` as freeform, so this is advice, not a
            # rejection — flag the likely typo without failing the manifest.
            warnings.append(
                f"{where} has unrecognised type '{ptype}' "
                f"(known types: {sorted(PARAM_TYPES)})"
            )

        choices = param.get("choices")
        if choices is not None and not isinstance(choices, list):
            errors.append(f"{where} choices must be an array")

        for bound in ("min", "max"):
            if bound in param:
                value = param[bound]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"{where} {bound} must be a number")


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

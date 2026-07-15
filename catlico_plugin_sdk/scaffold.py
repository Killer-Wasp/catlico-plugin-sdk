"""Scaffolding for ``catlico-plugin new`` — generates a fresh, valid plugin tree.

Kept separate from ``cli.py`` (which stays a thin argparse + dispatch layer) so the
id/package derivation and the file templates have one place to live and one thing
to test. The generated tree is a full uv project the runner can sync and run:

    <plugin-id>/
      main.py                 # entrypoint: `catlico = Catlico()` + @catlico.event
      catlico-plugin.toml     # entrypoint = "main:catlico"
      pyproject.toml
      src/<pkg>/__init__.py
      src/<pkg>/plugin.py     # module-level async process(event, ctx) / health(ctx)
      tests/test_plugin.py
      .github/workflows/ci.yml

The generated ``catlico-plugin.toml`` is built to pass
``catlico_plugin_sdk.manifest.validate_manifest`` with an empty error list and
``manifest_warnings`` with no warnings out of the box.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

#: A plugin id becomes a directory name and (via derive_package_name) part of a
#: Python identifier, so keep it restricted: lowercase-ish letters/digits, with
#: internal hyphens/underscores, starting with a letter. This mirrors the shape
#: of real plugin ids in the repo (``abuseipdb``, ``my-cool-plugin``).
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: GitHub Actions CI for a generated plugin: the runner's own gates —
#: ``uv sync --frozen`` (the committed lock resolves), the plugin's tests, and
#: ``catlico-plugin validate`` (manifest + trigger/handler agreement) — so a
#: pushed plugin fails in CI, not at install time.
_CI_WORKFLOW = """\
name: CI

# Sync (frozen), test, and validate a Catlico plugin.
on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.14"

      - name: Sync (frozen)
        run: uv sync --frozen

      - name: Test
        run: uv run pytest

      - name: Validate the manifest
        run: uv run catlico-plugin validate .
"""


def derive_package_name(plugin_id: str) -> str:
    """``<plugin-id>`` -> the importable package name ``<pkg>_plugin``.

    Hyphens and other non-identifier characters become underscores, e.g.
    ``abuseipdb`` -> ``abuseipdb_plugin``, ``my-cool-plugin`` ->
    ``my_cool_plugin_plugin``.
    """
    stem = re.sub(r"[^0-9a-zA-Z_]+", "_", plugin_id).strip("_") or "plugin"
    if stem[0].isdigit():
        stem = f"_{stem}"
    return f"{stem}_plugin"


def scaffold_plugin(
    plugin_id: str,
    parent_dir: str | Path,
    *,
    name: str | None = None,
) -> Path:
    """Generate a fresh plugin tree at ``<parent_dir>/<plugin_id>/``.

    Returns the created plugin directory. Raises ``ValueError`` for an invalid
    ``plugin_id`` and ``FileExistsError`` if the target directory already exists
    and is non-empty (an empty existing directory is fine — it is filled in).
    """
    if not _ID_RE.match(plugin_id):
        raise ValueError(
            f"invalid plugin id {plugin_id!r}: must start with a letter and "
            "contain only letters, digits, hyphens, and underscores"
        )

    target = Path(parent_dir) / plugin_id
    # ``iterdir`` raises NotADirectoryError if the path is a regular file, which
    # new_command does not catch — pre-check for a clean error either way.
    if target.is_file():
        raise FileExistsError(f"target path already exists as a file: {target}")
    if target.is_dir() and any(target.iterdir()):
        raise FileExistsError(
            f"target directory already exists and is not empty: {target}"
        )

    package = derive_package_name(plugin_id)
    display_name = name or plugin_id

    src_dir = target / "src" / package
    src_dir.mkdir(parents=True, exist_ok=True)
    (target / "tests").mkdir(parents=True, exist_ok=True)
    workflows_dir = target / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    (target / "catlico-plugin.toml").write_text(
        _manifest_toml(plugin_id, display_name)
    )
    (target / "pyproject.toml").write_text(_pyproject_toml(plugin_id, package, display_name))
    (target / "main.py").write_text(_main_py(display_name, package))
    (workflows_dir / "ci.yml").write_text(_CI_WORKFLOW)
    (src_dir / "__init__.py").write_text("")
    (src_dir / "plugin.py").write_text(_plugin_py(display_name))
    (target / "tests" / "test_plugin.py").write_text(_test_py(package, display_name))

    return target


def _toml_str(value: str) -> str:
    """A double-quoted TOML basic string literal for ``value``.

    ``json.dumps`` and TOML basic-string escaping agree on the characters that
    matter here (``"``, ``\\``, control chars), so it's a safe, simple way to
    embed an arbitrary id/name/description without hand-rolling TOML escaping.
    """
    return json.dumps(value)


def _manifest_toml(plugin_id: str, display_name: str) -> str:
    return f"""\
id = {_toml_str(plugin_id)}
name = {_toml_str(display_name)}
version = "0.1.0"
description = {_toml_str(f"TODO: describe what {display_name} does.")}
sdk = ">=0.1,<1"
runtime = "python"
entrypoint = "main:catlico"
capabilities = ["enrichment"]
triggers = ["observable.created"]
permissions = ["read:observable", "write:observable_enrichment"]
timeout_seconds = 30

[[configuration]]
name = "api_key"
type = "string"
secret = true
required = true
description = "API key or credential this plugin needs. TODO: rename or remove."

[[configuration]]
name = "threshold"
type = "integer"
required = false
defaultValue = 50
description = "Example numeric parameter. TODO: rename, retype, or remove."
"""


def _pyproject_toml(plugin_id: str, package: str, display_name: str) -> str:
    return f"""\
[project]
name = "catlico-{plugin_id}-plugin"
version = "0.1.0"
description = {_toml_str(f"TODO: describe what {display_name} does.")}
requires-python = ">=3.14"
dependencies = [
    "catlico-plugin-sdk",
]

[dependency-groups]
dev = [
    "pytest>=9.1.0",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{package}"]

# Dev-only source for local `uv` work in the monorepo, where the SDK is an
# unpublished sibling checkout. A standalone plugin repo replaces this with a git
# pin, e.g.:
#   catlico-plugin-sdk = {{ git = "https://github.com/Killer-Wasp/catlico-plugin-sdk", rev = "<sha>" }}
[tool.uv.sources]
catlico-plugin-sdk = {{ path = "../../catlico-plugin-sdk" }}

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src", "."]
"""


def _main_py(display_name: str, package: str) -> str:
    return f"""\
\"\"\"{display_name} — the Catlico plugin entrypoint.

The runner imports ``catlico`` (entrypoint = "main:catlico") and dispatches
events to the handlers below, which delegate to the plain functions in
``src/{package}/plugin.py`` — that's where your logic lives.
\"\"\"
from catlico_plugin_sdk import Catlico

from {package} import plugin

catlico = Catlico()


@catlico.event("observable.created")
async def on_observable_created(event, ctx):
    # A ``matchers=[...]`` argument on the decorator is the place for cheap
    # envelope filters, e.g. only IPs:
    #   @catlico.event("observable.created",
    #                  matchers=[lambda e: e.data.get("observable_type") == "ip"])
    await plugin.process(event, ctx)


@catlico.health()
async def health(ctx):
    return await plugin.health(ctx)
"""


def _plugin_py(display_name: str) -> str:
    return f"""\
\"\"\"{display_name} plugin logic — generated by `catlico-plugin new`.

Plain module-level async functions: ``main.py`` wires them to events. Replace the
TODOs with your real logic. Testable directly against a ``FakeContext`` (see
``tests/test_plugin.py``).
\"\"\"
from __future__ import annotations

from catlico_plugin_sdk.plugin import InputError


async def health(ctx) -> dict:
    # TODO: check any credentials/config this plugin needs (e.g.
    # ctx.secrets["api_key"]) and raise ConfigError if missing/unusable.
    return {{"ok": True}}


async def process(event, ctx) -> None:
    value = (event.data.get("data") or "").strip()
    if not value:
        raise InputError("observable has no value")

    # TODO: replace this with your real enrichment logic — e.g. a vendor call via
    # ``ctx.http``, then a verdict derived from the response. This stub just
    # records that the plugin ran, so a first `catlico-plugin run` has something
    # to show. Raise ``SkipRun(...)`` (from catlico_plugin_sdk) to skip an event
    # after inspecting it.
    await ctx.api.add_observable_enrichment(
        event.object_id,
        source={display_name!r},
        data={{"value": value}},
        verdict="info",
        summary={display_name!r} + f" processed {{value}}",
    )
"""


def _test_py(package: str, display_name: str) -> str:
    return f"""\
\"\"\"Starter test for {display_name!r}, generated by `catlico-plugin new`.

TODO: replace/expand this with real coverage of process().
\"\"\"
from catlico_plugin_sdk.testing import FakeContext, observable_event

from {package} import plugin


async def test_process_writes_an_enrichment():
    ctx = FakeContext(permissions={{"read:observable", "write:observable_enrichment"}})
    await plugin.process(observable_event(data="1.2.3.4"), ctx)
    assert ctx.results
    assert ctx.results[0]["source"] == {display_name!r}
"""


__all__ = [
    "derive_package_name",
    "scaffold_plugin",
]

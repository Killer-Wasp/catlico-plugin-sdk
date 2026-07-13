"""Scaffolding for ``catlico-plugin new`` — generates a fresh, valid plugin tree.

Kept separate from ``cli.py`` (which stays a thin argparse + dispatch layer) so the
id/package/class derivation and the file templates — modeled on
``catlico-plugins/abuseipdb``, the SDK's reference plugin — have one place to live
and one thing to test. The generated ``catlico-plugin.toml`` is built to pass
``catlico_plugin_sdk.manifest.validate_manifest`` with an empty error list and
``manifest_warnings`` with no warnings out of the box.
"""
from __future__ import annotations

import json
import keyword
import re
from pathlib import Path

#: A plugin id becomes a directory name and (via derive_package_name) part of a
#: Python identifier, so keep it restricted: lowercase-ish letters/digits, with
#: internal hyphens/underscores, starting with a letter. This mirrors the shape
#: of real plugin ids in the repo (``abuseipdb``, ``my-cool-plugin``).
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

_DOCKERFILE = """\
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv
RUN useradd --uid 65534 --no-create-home nobodyplugin || true
WORKDIR /plugin
COPY . /plugin
RUN pip install --no-cache-dir /plugin/.catlico-sdk && rm -rf /plugin/.catlico-sdk
RUN uv export --frozen --no-dev --no-emit-project --no-emit-package catlico-plugin-sdk --no-hashes -o /tmp/deps.txt \\
 && uv pip install --system --no-cache -r /tmp/deps.txt \\
 && rm -f /tmp/deps.txt
ENV PYTHONPATH=/plugin/src:/plugin
USER 65534:65534
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


def derive_class_name(plugin_id: str) -> str:
    """``<plugin-id>`` -> a CamelCase ``...Plugin`` class name.

    e.g. ``abuseipdb`` -> ``AbuseipdbPlugin``, ``my-cool-plugin`` ->
    ``MyCoolPluginPlugin`` (each hyphen-separated segment is title-cased, then
    ``Plugin`` is appended).
    """
    segments = [s for s in re.split(r"[^0-9a-zA-Z]+", plugin_id) if s]
    camel = "".join(s.capitalize() for s in segments) or "Plugin"
    if camel[0].isdigit():
        camel = f"P{camel}"
    return f"{camel}Plugin"


def scaffold_plugin(
    plugin_id: str,
    parent_dir: str | Path,
    *,
    name: str | None = None,
    class_name: str | None = None,
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

    # A supplied class name is spliced verbatim into ``class X(...)`` and the
    # manifest entrypoint, so it must be a valid, non-keyword Python identifier —
    # otherwise the generated plugin.py is a SyntaxError while the CLI exits 0.
    if class_name is not None and (
        not class_name.isidentifier() or keyword.iskeyword(class_name)
    ):
        raise ValueError(
            f"invalid class name {class_name!r}: must be a valid Python "
            "identifier and not a reserved keyword"
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
    cls = class_name or derive_class_name(plugin_id)
    display_name = name or plugin_id

    src_dir = target / "src" / package
    src_dir.mkdir(parents=True, exist_ok=True)
    (target / "tests").mkdir(parents=True, exist_ok=True)

    (target / "catlico-plugin.toml").write_text(
        _manifest_toml(plugin_id, display_name, package, cls)
    )
    (target / "pyproject.toml").write_text(_pyproject_toml(plugin_id, package, display_name))
    (target / "Dockerfile.catlico").write_text(_DOCKERFILE)
    (src_dir / "__init__.py").write_text(_init_py(package, cls))
    (src_dir / "plugin.py").write_text(_plugin_py(display_name, cls))
    (target / "tests" / "test_plugin.py").write_text(_test_py(package, cls, display_name))

    return target


def _toml_str(value: str) -> str:
    """A double-quoted TOML basic string literal for ``value``.

    ``json.dumps`` and TOML basic-string escaping agree on the characters that
    matter here (``"``, ``\\``, control chars), so it's a safe, simple way to
    embed an arbitrary id/name/description without hand-rolling TOML escaping.
    """
    return json.dumps(value)


def _manifest_toml(plugin_id: str, display_name: str, package: str, cls: str) -> str:
    return f"""\
id = {_toml_str(plugin_id)}
name = {_toml_str(display_name)}
version = "0.1.0"
description = {_toml_str(f"TODO: describe what {display_name} does.")}
sdk = ">=0.1,<1"
runtime = "python"
entrypoint = {_toml_str(f"{package}.plugin:{cls}")}
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

[project.optional-dependencies]
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

[tool.uv.sources]
catlico-plugin-sdk = {{ path = "../../catlico-plugin-sdk" }}

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src"]
"""


def _init_py(package: str, cls: str) -> str:
    return f"""\
from {package}.plugin import {cls}

__all__ = ["{cls}"]
"""


def _plugin_py(display_name: str, cls: str) -> str:
    return f"""\
\"\"\"{display_name!r} plugin — generated by `catlico-plugin new`.

This is a minimal, runnable starting point: it subclasses ``CatlicoPlugin``,
declares a trigger, and writes a trivial observable enrichment so the plugin is
valid and exercisable via ``catlico-plugin run`` right away. Replace the TODOs
below with your real logic.
\"\"\"
from __future__ import annotations

from catlico_plugin_sdk import CatlicoPlugin
from catlico_plugin_sdk.plugin import InputError


class {cls}(CatlicoPlugin):
    triggers = ["observable.created"]

    async def health(self, ctx) -> dict:
        # TODO: check any credentials/config this plugin needs (e.g.
        # ctx.secrets["api_key"]) and raise ConfigError if missing.
        return {{"ok": True}}

    async def should_process(self, event, ctx) -> bool:
        # Cheap, synchronous filter — return False to skip without a run.
        # TODO: narrow this to the observable/event shapes you actually handle.
        return event.event_type in self.triggers

    async def process(self, event, ctx) -> None:
        value = (event.data.get("data") or "").strip()
        if not value:
            raise InputError("observable has no value")

        # TODO: replace this with your real enrichment logic — e.g. a vendor
        # call via ``ctx.http``, then a verdict derived from the response. This
        # stub just records that the plugin ran, so a first `catlico-plugin
        # run` has something to show.
        await ctx.api.add_observable_enrichment(
            event.object_id,
            source={display_name!r},
            data={{"value": value}},
            verdict="info",
            summary={display_name!r} + f" processed {{value}}",
        )
"""


def _test_py(package: str, cls: str, display_name: str) -> str:
    return f"""\
\"\"\"Starter test for {display_name!r}, generated by `catlico-plugin new`.

TODO: replace/expand this with real coverage of process().
\"\"\"
from catlico_plugin_sdk.testing import FakeContext, observable_event

from {package}.plugin import {cls}


async def test_process_writes_an_enrichment():
    ctx = FakeContext(permissions={{"read:observable", "write:observable_enrichment"}})
    await {cls}().process(observable_event(data="1.2.3.4"), ctx)
    assert ctx.results
    assert ctx.results[0]["source"] == {display_name!r}
"""


__all__ = [
    "derive_class_name",
    "derive_package_name",
    "scaffold_plugin",
]

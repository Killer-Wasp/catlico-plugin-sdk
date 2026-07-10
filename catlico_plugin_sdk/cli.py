"""``catlico-plugin`` command-line interface.

Two subcommands, both offline (no live Catlico API, no runner):

* ``catlico-plugin validate [PATH]`` — validate a plugin's ``catlico-plugin.toml``
  manifest: schema, required fields, and config parameter declarations.
* ``catlico-plugin run --event EVENT.json [PATH]`` — execute a plugin locally
  against an event envelope using the fake runtime context, then print the
  emitted results, progress, and any permission violations.

``run`` mirrors the sandbox worker's invocation path
(``catlico_plugin_sdk._worker``): it imports the plugin via its manifest
entrypoint, parses the envelope into a ``PluginEvent``, and drives
``should_process`` → ``process`` — but against a ``FakeContext`` instead of a
live runtime.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import traceback
from pathlib import Path
from typing import TextIO

from catlico_plugin_sdk.manifest import (
    MANIFEST_FILENAME,
    load_manifest,
    manifest_defaults,
    manifest_warnings,
    validate_manifest,
)
from catlico_plugin_sdk.models import PluginEvent
from catlico_plugin_sdk.plugin import PluginRuntimeError
from catlico_plugin_sdk.testing import FakeContext


def _plugin_dir(path: str | Path) -> Path:
    p = Path(path)
    return p.parent if p.is_file() else p


def _source_path(directory: Path) -> str:
    """Where the plugin's importable package lives (standard layout uses src/)."""
    src = directory / "src"
    return str(src if src.is_dir() else directory)


def _load_json(path: str | None) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text())


def validate_command(path: str, *, out: TextIO = sys.stdout) -> int:
    """Validate a manifest. Returns a process exit code (0 == valid)."""
    manifest_path = _plugin_dir(path) / MANIFEST_FILENAME
    if not manifest_path.is_file():
        print(f"error: no {MANIFEST_FILENAME} found at {manifest_path}", file=out)
        return 1
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001 — malformed TOML is a validation failure
        print(f"error: could not parse {manifest_path}: {exc}", file=out)
        return 1

    warnings = manifest_warnings(manifest)
    if warnings:
        print(f"{manifest_path}: {len(warnings)} warning(s)", file=out)
        for warning in warnings:
            print(f"  - warning: {warning}", file=out)

    errors = validate_manifest(manifest)
    if errors:
        print(f"{manifest_path}: {len(errors)} error(s)", file=out)
        for error in errors:
            print(f"  - {error}", file=out)
        return 1

    name = manifest.get("name") or manifest.get("id")
    print(f"ok: {name} {manifest.get('version', '')} manifest is valid", file=out)
    return 0


def _load_plugin(manifest: dict, directory: Path):
    entrypoint = manifest.get("entrypoint", "")
    module_name, _, class_name = entrypoint.partition(":")
    source = _source_path(directory)
    if source not in sys.path:
        sys.path.insert(0, source)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


async def _drive(plugin, event: PluginEvent, ctx: FakeContext) -> dict:
    """Run should_process → process, classifying failures like the worker does."""
    try:
        if not await plugin.should_process(event, ctx):
            return {
                "status": "skipped",
                "skip_reason": "should_process returned False",
            }
        await plugin.process(event, ctx)
        return {"status": "success"}
    except PluginRuntimeError as exc:
        return {
            "status": "failure",
            "error": f"{type(exc).__name__}: {exc}",
            "error_kind": getattr(exc, "error_kind", "bug"),
        }
    except Exception as exc:  # noqa: BLE001 — any plugin failure becomes a result
        return {
            "status": "failure",
            "error": f"{type(exc).__name__}: {exc}",
            "error_kind": "bug",
            "traceback": traceback.format_exc(),
        }
    finally:
        if ctx.http is not None:
            await ctx.http.aclose()


def _print_outcome(result: dict, ctx: FakeContext, *, out: TextIO) -> None:
    print(f"status: {result['status']}", file=out)
    if result.get("skip_reason"):
        print(f"skip_reason: {result['skip_reason']}", file=out)
    if result.get("error"):
        print(f"error_kind: {result.get('error_kind', 'bug')}", file=out)
        print(f"error: {result['error']}", file=out)
        if result.get("traceback"):
            print(result["traceback"], file=out)

    print(f"\nresults ({len(ctx.results)}):", file=out)
    for record in ctx.results:
        print(f"  - {json.dumps(record, default=str)}", file=out)

    print(f"\nprogress ({len(ctx.progress_updates)}):", file=out)
    for update in ctx.progress_updates:
        pct = update.get("percent")
        suffix = f" ({pct}%)" if pct is not None else ""
        print(f"  - {update['message']}{suffix}", file=out)

    if ctx.uploaded_files:
        print(f"\nuploaded files ({len(ctx.uploaded_files)}):", file=out)
        for f in ctx.uploaded_files:
            print(f"  - {f['filename']} ({f['size']} bytes, {f['sha256'][:12]}…)", file=out)

    if ctx.permission_denials:
        print(f"\npermission violations ({len(ctx.permission_denials)}):", file=out)
        for denial in ctx.permission_denials:
            print(f"  - {denial}", file=out)


def run_command(
    path: str,
    event_path: str,
    *,
    config_path: str | None = None,
    secrets_path: str | None = None,
    out: TextIO = sys.stdout,
) -> int:
    """Execute a plugin against an event envelope. Returns a process exit code."""
    directory = _plugin_dir(path)
    manifest_path = directory / MANIFEST_FILENAME
    if not manifest_path.is_file():
        print(f"error: no {MANIFEST_FILENAME} found at {manifest_path}", file=out)
        return 1

    manifest = load_manifest(manifest_path)
    for warning in manifest_warnings(manifest):
        print(f"warning: {warning}", file=out)
    errors = validate_manifest(manifest)
    if errors:
        print("error: manifest is invalid — fix it before running:", file=out)
        for error in errors:
            print(f"  - {error}", file=out)
        return 1

    try:
        envelope = json.loads(Path(event_path).read_text())
        event = PluginEvent.from_envelope(envelope)
    except (OSError, ValueError) as exc:
        print(f"error: could not load event from {event_path}: {exc}", file=out)
        return 1

    config = manifest_defaults(manifest)
    config.update(_load_json(config_path))
    secrets = _load_json(secrets_path)

    try:
        plugin = _load_plugin(manifest, directory)
    except Exception as exc:  # noqa: BLE001 — import/entrypoint failure
        print(f"error: could not load plugin: {exc}", file=out)
        return 1

    # Real outbound HTTP (as the worker wires it) so vendor calls behave normally;
    # only the Catlico API is faked.
    from catlico_plugin_sdk.api import PluginHttp

    ctx = FakeContext(
        manifest=manifest,
        config=config,
        secrets=secrets,
        http=PluginHttp(),
        organisation_id=event.organisation_id or "org-fake",
        event_id=event.event_id or "audit:fake",
    )
    result = asyncio.run(_drive(plugin, event, ctx))
    _print_outcome(result, ctx, out=out)
    return 0 if result["status"] in ("success", "skipped") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catlico-plugin",
        description="Develop and test Catlico plugins locally.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser(
        "validate", help="validate a plugin's catlico-plugin.toml manifest"
    )
    p_validate.add_argument(
        "path",
        nargs="?",
        default=".",
        help="plugin directory or manifest file (default: current directory)",
    )

    p_run = sub.add_parser(
        "run", help="run a plugin locally against an event envelope"
    )
    p_run.add_argument(
        "path",
        nargs="?",
        default=".",
        help="plugin directory or manifest file (default: current directory)",
    )
    p_run.add_argument(
        "--event",
        required=True,
        dest="event",
        help="path to a JSON event envelope",
    )
    p_run.add_argument(
        "--config", dest="config", help="path to a JSON config fixture"
    )
    p_run.add_argument(
        "--secrets", dest="secrets", help="path to a JSON secrets fixture"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        return validate_command(args.path)
    if args.command == "run":
        return run_command(
            args.path,
            args.event,
            config_path=args.config,
            secrets_path=args.secrets,
        )
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

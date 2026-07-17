"""Plugin execution worker — the plugin run entrypoint.

Run as ``<plugin-venv>/bin/python -m catlico_plugin_sdk._worker``: the runner
spawns this with the plugin's OWN venv interpreter, so the plugin's dependencies
come solely from that venv's isolated site-packages. The runner's site-packages
are NEVER on this process's path — a StackStorm-class bug (a missing plugin
import silently falling back to a host dependency) is impossible by construction.

It reads one JSON request from stdin, executes the plugin in this process (never
in the long-running runner), and writes the result JSON to ``result_path``. The
plugin's stdout/stderr flow to this process's streams, which the parent captures
as the run's log tail. This module writes nothing to stdout.

DANGER: never import two plugins' ``main`` modules in one interpreter. Every
plugin's entrypoint module is named ``main`` by convention, so co-importing two
would collide by design. The model is strictly one process per run — do not batch.
"""
import asyncio
import importlib
import json
import sys
import traceback

from catlico_plugin_sdk.app import _error_result


def _load_request() -> dict:
    return json.loads(sys.stdin.read() or "{}")


def _load_secrets(request: dict) -> dict:
    """Resolve the run's secrets.

    Secrets are delivered in-band as ``secrets`` in the stdin payload (plain
    subprocess execution — the runner and worker share a trust boundary). A
    ``secrets_path`` that is set but missing/unreadable raises here (a clear
    harness failure) rather than silently running with no secrets.
    """
    secrets_path = request.get("secrets_path")
    if secrets_path:
        with open(secrets_path) as fh:
            return json.load(fh)
    return request.get("secrets", {})


def _load_app(request: dict):
    """Import the plugin's ``Catlico`` app from its manifest entrypoint.

    ``plugin_path`` is the plugin ROOT (where ``main.py`` lives). The plugin's
    ``src/`` package is installed into the venv (uv installs the project), so only
    the root goes on ``sys.path`` — deps come from the venv, never from here.
    """
    plugin_path = request.get("plugin_path")
    if plugin_path and plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)
    module = importlib.import_module(request["plugin_module"])
    return getattr(module, request["plugin_object"])


def _build_context(request: dict, event):
    from catlico_plugin_sdk import (
        PluginApiClient,
        PluginContext,
        PluginHttp,
    )

    api = None
    api_base_url = request.get("api_base_url")
    if api_base_url:
        api = PluginApiClient(api_base_url, request.get("run_token", ""))
    ctx = PluginContext(
        run_id=request.get("run_id", ""),
        plugin_id=request.get("plugin_id", ""),
        plugin_version=request.get("plugin_version", ""),
        organisation_id=event.organisation_id,
        event_id=event.event_id,
        permissions=set(request.get("permissions", [])),
        api=api,
        http=PluginHttp(),
        config=request.get("config", {}),
        secrets=_load_secrets(request),
    )
    return ctx, api


async def _run(request: dict) -> dict:
    from catlico_plugin_sdk import Catlico, PluginEvent

    run_id = request.get("run_id", "")

    app = _load_app(request)
    if not isinstance(app, Catlico):
        return {
            "run_id": run_id,
            "status": "failure",
            "error": (
                f"entrypoint {request.get('plugin_module')}:{request.get('plugin_object')} "
                f"is not a catlico_plugin_sdk.Catlico app (got {type(app).__name__})"
            ),
            "error_kind": "config",
        }

    # Strict equality: the manifest's declared triggers must match the app's
    # registered events exactly, so routing metadata and code cannot drift.
    declared = set(request.get("declared_triggers", []))
    registered = set(app.registered_events)
    if declared != registered:
        return {
            "run_id": run_id,
            "status": "failure",
            "error": (
                "manifest triggers do not match registered @catlico.event handlers: "
                f"manifest={sorted(declared)} handlers={sorted(registered)}"
            ),
            "error_kind": "config",
        }

    action = request.get("action", "event")
    event = PluginEvent.from_envelope(request.get("event", {}))
    ctx, api = _build_context(request, event)
    try:
        if action == "health":
            try:
                health = await app.check_health(ctx)
            except Exception as exc:  # noqa: BLE001 — classify like a run failure
                traceback.print_exc()
                return {"run_id": run_id, **_error_result(exc)}
            return {"run_id": run_id, "status": "success", "health": health}
        result = await app.dispatch(event, ctx)
        return {"run_id": run_id, **result}
    finally:
        if api is not None:
            await api.aclose()
        if ctx.http is not None:
            await ctx.http.aclose()


def _write_result(result_path: str, result: dict) -> None:
    """Write the run result, degrading to a serializable failure if it won't encode.

    The result carries plugin-authored values — a health check's return dict, a
    ``SkipRun`` reason — so encoding can fail on a live object (a ``datetime``, a
    vendor SDK type). Letting that escape is the worst outcome available: the
    result file ends up absent or half-written, and ``Executor._read_result``
    treats both the same, reporting the useless "plugin process produced no
    result" while the real error survives only in the log tail. Reporting the
    encoding failure *as* the run's error keeps the diagnosis in the run record.

    Encoding fully before opening the file is what makes that guarantee hold: a
    failure mid-``json.dump`` would leave truncated JSON the runner cannot parse.
    """
    try:
        payload = json.dumps(result)
    except (TypeError, ValueError) as exc:  # unserializable value, or circular ref
        traceback.print_exc()
        payload = json.dumps(
            {
                "run_id": str(result.get("run_id", "")),
                "status": "failure",
                "error": f"plugin result is not JSON-serializable: {type(exc).__name__}: {exc}",
                "error_kind": "bug",
            }
        )
    with open(result_path, "w") as fh:
        fh.write(payload)


def main() -> None:
    request = _load_request()
    result_path = request.get("result_path")
    try:
        result = asyncio.run(_run(request))
    except Exception as exc:  # noqa: BLE001 — harness failure, still report
        traceback.print_exc()
        result = {
            "run_id": request.get("run_id", ""),
            "status": "failure",
            "error": f"worker harness error: {type(exc).__name__}: {exc}",
            "error_kind": "bug",
        }
    if result_path:
        _write_result(result_path, result)


if __name__ == "__main__":
    main()

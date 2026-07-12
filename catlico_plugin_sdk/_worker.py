"""Sandbox worker — the plugin execution entrypoint.

Run as ``python -m catlico_plugin_sdk._worker`` inside an isolated environment
(a subprocess or a container). Reads one JSON request from stdin, executes the
plugin in this process (never in the long-running runner), and writes the result
JSON to ``result_path``. The plugin's stdout/stderr flow to this process's
streams, which the parent captures as the run's log tail. This module writes
nothing to stdout.

Living in the SDK means any environment that has the SDK installed (a trusted
subprocess or a built plugin image) shares one entrypoint.
"""
import asyncio
import importlib
import json
import sys
import traceback

#: When no ``result_path`` is given (container mode), the result JSON is emitted
#: on stdout on its own line behind this marker; the parent splits it from the
#: plugin's log output.
RESULT_SENTINEL = "__CATLICO_RESULT__"


def _load_request() -> dict:
    return json.loads(sys.stdin.read() or "{}")


def _load_secrets(request: dict) -> dict:
    """Resolve the run's secrets.

    Container mode delivers secrets out-of-band: the runner writes them to a
    read-only bind-mounted file and passes its in-container path as
    ``secrets_path``. Subprocess (trusted-local) mode still sends them in-band as
    ``secrets`` in the stdin payload. Prefer ``secrets_path`` when present; fall
    back to in-band ``secrets`` otherwise.

    A ``secrets_path`` that is set but missing/unreadable/malformed raises here
    (surfacing as a clear harness failure) rather than silently running the
    plugin with no secrets.
    """
    secrets_path = request.get("secrets_path")
    if secrets_path:
        with open(secrets_path) as fh:
            return json.load(fh)
    return request.get("secrets", {})


async def _run(request: dict) -> dict:
    run_id = request.get("run_id", "")
    plugin_path = request.get("plugin_path")
    if plugin_path and plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)

    from catlico_plugin_sdk import (
        PluginApiClient,
        PluginContext,
        PluginEvent,
        PluginHttp,
        PluginRuntimeError,
    )

    module = importlib.import_module(request["plugin_module"])
    plugin_cls = getattr(module, request["plugin_class"])
    plugin = plugin_cls()

    event = PluginEvent.from_envelope(request.get("event", {}))
    api = None
    api_base_url = request.get("api_base_url")
    if api_base_url:
        api = PluginApiClient(api_base_url, request.get("run_token", ""))
    ctx = PluginContext(
        run_id=run_id,
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

    try:
        should = await plugin.should_process(event, ctx)
        if not should:
            return {
                "run_id": run_id,
                "status": "skipped",
                "skip_reason": "should_process returned False",
            }
        await plugin.process(event, ctx)
        return {"run_id": run_id, "status": "success"}
    except PluginRuntimeError as exc:
        return {
            "run_id": run_id,
            "status": "failure",
            "error": f"{type(exc).__name__}: {exc}",
            "error_kind": getattr(exc, "error_kind", "bug"),
        }
    except Exception as exc:  # noqa: BLE001 — any plugin failure becomes a result
        traceback.print_exc()
        return {
            "run_id": run_id,
            "status": "failure",
            "error": f"{type(exc).__name__}: {exc}",
            "error_kind": "bug",
        }
    finally:
        if api is not None:
            await api.aclose()
        if ctx.http is not None:
            await ctx.http.aclose()


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
            "error": f"sandbox harness error: {type(exc).__name__}: {exc}",
            "error_kind": "bug",
        }
    if result_path:
        with open(result_path, "w") as fh:
            json.dump(result, fh)
    else:
        # Container mode: emit the result on stdout behind the sentinel.
        sys.stdout.write(f"\n{RESULT_SENTINEL}{json.dumps(result)}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

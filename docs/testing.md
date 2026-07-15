# Testing plugins

`catlico_plugin_sdk.testing` is a fake runtime with no live Catlico API and no network. It is
**not** re-exported from the package top level — import it directly:

```python
from catlico_plugin_sdk.testing import FakeContext, observable_event, fake_http, run_app
```

## `FakeContext`

A real `PluginContext` subclass backed by `FakeCatlicoApi` — a genuine drop-in.

Its key property: **it enforces the same manifest permissions production enforces.** Pass your
manifest and permissions are derived from `manifest["permissions"]`; a call your manifest
didn't declare raises `PermissionDenied`, the offline equivalent of the runtime's 403.

What it records, for assertions:

| Attribute | Contents |
|---|---|
| `ctx.results` | enrichments / results / proposed actions the plugin emitted |
| `ctx.uploaded_files` | everything passed to `upload_file` |
| `ctx.progress_updates` | every `progress(...)` call |
| `ctx.permission_denials` | every attempted over-reach |

`ctx.assert_no_permission_violations()` fails the test if any denial was recorded.

Seed reads with `cases=`, `alerts=`, `observables=`, `downloads=` — an unseeded read raises
`LookupError`, mirroring a 404. Supply `config=` / `secrets=` directly.

## Event factories

`observable_event(...)`, `case_event(...)`, and `alert_event(...)` build valid envelopes
(`make_envelope(...)` gives you the raw dict).

## `fake_http(handler)`

Returns a `PluginHttp` whose requests are served by your handler — return a `403` to exercise
`ConfigError`, a `500`/`429` to exercise `TransientError`.

## `run_app(app, event, ctx)`

Dispatches `event` through a whole `Catlico` app (matchers + handlers + error classification)
and returns the run's result dict — `{"status": "success" | "skipped" | "failure", ...}` — the
same shape the runner sees. Use it to assert on end-to-end routing (did a matcher skip? did the
right `error_kind` come back?); call a single handler function directly when you only need to
unit-test one piece of logic.

## Example

```python
import httpx
from catlico_plugin_sdk.testing import FakeContext, observable_event, fake_http

MANIFEST = {
    "id": "demo",
    "version": "0.1.0",
    "permissions": ["read:observable", "write:observable_enrichment"],
}


async def test_enriches_ip():
    ctx = FakeContext(
        manifest=MANIFEST,                                  # permissions from here
        secrets={"key": "test-key"},
        http=fake_http(lambda r: httpx.Response(200, json={"score": 90})),
    )
    await enrich(observable_event(data="1.2.3.4"), ctx)  # your handler function

    assert ctx.results[0]["verdict"] == "malicious"
    ctx.assert_no_permission_violations()
    await ctx.http.aclose()
```

If the handler tried `ctx.api.get_case(1)` without `read:case` in the manifest, that call
raises `PermissionDenied` and the test fails — exactly as the runtime would reject it.

The assertions above assume a plugin that actually emits a `malicious` verdict; adapt them to
yours. `tests/test_testing.py` in this repo has complete working versions.

Tests are `async`. This repo runs them under `pytest-asyncio` with
`asyncio_mode = "auto"`, so no `@pytest.mark.asyncio` marker is needed. Close the real HTTP
client in teardown (`await ctx.http.aclose()`).

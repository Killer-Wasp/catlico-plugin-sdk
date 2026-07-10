# catlico-plugin-sdk

The authoring SDK for **Catlico plugins**: the `CatlicoPlugin` base class, the event
model, the runtime context, a manifest validator, a local test kit, and the
`catlico-plugin` CLI.

## What this is, and where a plugin runs

A Catlico plugin is a small Python package that reacts to Catlico events (an
observable was created, a case was opened, …) and writes back *evidence* —
enrichments, results, proposed case edits. You write a plugin against this SDK;
at runtime the **`catlico-plugin-runner`** loads your package and executes it in
an isolated sandbox (a subprocess or container — see
`catlico_plugin_sdk._worker`, the sandbox entrypoint).

Two boundaries matter and the SDK enforces both:

- **Plugin code never touches the database and never calls the public
  `/api/v1` surface.** All reads and writes go through `ctx.api`, which speaks to
  Catlico's *internal* plugin-runtime API (`/api/internal/plugin-runtime/*`)
  using a short-lived, run-scoped token minted for that one run.
- **A plugin can only do what its manifest declares.** The run token carries the
  permissions from your `catlico-plugin.toml`; the runtime rejects any call
  outside them (HTTP 403). The test kit enforces the *same* rules offline, so an
  over-reach fails in your unit tests, not in production.

Requires **Python 3.14+**. Runtime dependencies: `httpx` and `pydantic`.

---

## Writing a plugin

Subclass `CatlicoPlugin` and override the hooks you need:

```python
from catlico_plugin_sdk import CatlicoPlugin, ConfigError, InputError


class MyPlugin(CatlicoPlugin):
    triggers = ["observable.created"]           # event types you want (required)

    async def health(self, ctx) -> dict:
        # Called on activation. Return a dict, or raise to fail activation.
        if not ctx.secrets.get("key"):
            raise ConfigError("API key is not configured")
        return {"ok": True}

    async def should_process(self, event, ctx) -> bool:
        # Cheap envelope filter — no vendor/API calls here. Return False to skip.
        return event.data.get("observable_type") == "ip"

    async def process(self, event, ctx) -> None:
        # The real work. Raise a PluginRuntimeError subclass to fail the run.
        ...
```

The runner calls these in order: `health` (on activation), then per event
`should_process` → (if it returns `True`) `process`. All three are `async`.

The base-class defaults (see `plugin.py`): `health` returns `{"ok": True}`,
`should_process` returns `event.event_type in self.triggers`, and `process`
raises `NotImplementedError` — so you must override `process`.

### The event

`process`/`should_process` receive a `PluginEvent` — a thin, already-parsed
envelope. Filter on its fields cheaply; fetch full entity detail through
`ctx.api` only when you actually need it.

| attribute | meaning |
|---|---|
| `event_id` | audit id of the event |
| `event_type` | e.g. `"observable.created"` |
| `organisation_id` | tenant the event belongs to |
| `object_type` / `object_id` | the entity the event is about (`"observable"`, its id) |
| `data` | event payload dict (e.g. `{"observable_type": "ip", "data": "1.2.3.4"}`) |
| `occurred_at`, `actor`, `context`, `request_id`, `attempt` | envelope metadata |

`attempt` starts at 1 and increases on retries — useful if a step must be
idempotent.

---

## The runtime context (`ctx`)

Every hook receives a context object. In production it is a `PluginContext`; in
tests it is a `FakeContext` (a subclass, so it is a real drop-in). Attributes:

- **`ctx.config`** — `dict` of non-secret configuration for this run (manifest
  `defaultValue`s merged with org-level overrides).
- **`ctx.secrets`** — `dict` of secret values (API keys, tokens). Kept separate
  from `config`; secret parameters never carry a default.
- **`ctx.permissions`** — the `set` of permission strings granted this run, and
  **`ctx.has_permission(name)`** to test one.
- **`ctx.progress(message, percent=None)`** — report progress (best-effort;
  a no-op if no API client is wired).
- **`ctx.api`** — the internal Catlico API client (below).
- **`ctx.http`** — the outbound HTTP helper for vendor calls (below).
- Plus run metadata: `ctx.run_id`, `ctx.plugin_id`, `ctx.plugin_version`,
  `ctx.organisation_id`, `ctx.event_id`.

### `ctx.api` — reading and writing Catlico entities

All methods are `async`. The permission each requires (enforced by the runtime,
and by the test fake) is shown alongside.

| method | permission |
|---|---|
| `get_observable(observable_id: str) -> dict` | `read:observable` |
| `get_case(case_id: int) -> dict` | `read:case` |
| `get_alert(alert_id: int) -> dict` | `read:alert` |
| `add_result(**body) -> dict` | `write:plugin_result` **or** `write:observable_enrichment` |
| `add_observable_enrichment(observable_id: str, *, source: str, data: dict, **body) -> dict` | `write:observable_enrichment` |
| `propose_case_patch(case_id: int, **fields) -> dict` | `write:case` |
| `propose_task(case_id: int, **fields) -> dict` | `write:task` |
| `propose_tag(case_id: int, tag: str) -> dict` | `write:case` |
| `progress(message: str, percent: int \| None = None) -> dict` | (none) |
| `upload_file(content: bytes, filename: str, content_type: str = "application/octet-stream") -> dict` | `write:plugin_result` **or** `write:observable_enrichment` |
| `download_file(file_ref: str) -> bytes` | (none — scoped by the run token) |

`add_result` requires `entity_type`, `entity_id`, and a truthy `fingerprint` in
its body; the runtime **dedups on `fingerprint` per run** (a repeat returns the
same id with `created: False`). The `propose_*` methods create *proposed
actions* that an analyst must approve — they do not mutate canonical data
directly.

### `ctx.http` — outbound vendor calls with automatic error classification

`ctx.http` is a thin `httpx.AsyncClient` wrapper (`request`, `get`, `get_json`,
`post`) that turns transport-level and HTTP failures into the SDK's typed
exceptions so retry semantics come for free (see `api.py`):

| condition | raises |
|---|---|
| response status `401` or `403` | `ConfigError` |
| response status `429`, or `>= 500` | `TransientError` |
| timeout (`httpx.TimeoutException`) | `TransientError` |
| other transport error (`httpx.TransportError`) | `TransientError` |

Any other status (including 2xx and, notably, other 4xx like 400/404) is
returned to you as a normal `httpx.Response` — inspect `resp.status_code`
yourself and raise if you want to fail. `ctx.http` does **not** enforce plugin
permissions; those apply only to `ctx.api`.

---

## Error handling

Raise one of these from `health`/`process` to fail a run. Each carries an
`error_kind` that the runner uses to classify the failure and decide whether to
retry:

| exception | `error_kind` | retried? | meaning |
|---|---|---|---|
| `TransientError` | `transient` | yes | temporary upstream failure (vendor 429/5xx, network) |
| `ConfigError` | `config` | no | config/credential invalid or unusable; trips a config circuit breaker after repeated failures |
| `InputError` | `input` | no | the event/entity is unsupported or malformed |
| `PluginRuntimeError` (base) | `bug` | no | anything else you raise deliberately |

Any *other* uncaught exception is treated as a `bug` as well. Because `ctx.http`
already maps `401/403 → ConfigError` and `429/5xx/network → TransientError`, a
plugin that just calls `ctx.http` gets sensible retry behaviour without writing
any of this by hand.

`ConfigError`, `TransientError`, `InputError`, and `PluginRuntimeError` are all
importable from the package top level (`from catlico_plugin_sdk import
ConfigError`).

---

## The manifest — `catlico-plugin.toml`

A plugin declares its identity, triggers, permissions, and configurable
parameters in `catlico-plugin.toml` at the plugin root. Top-level fields the SDK
reads (validator in `manifest.py`; `name` is used by the `validate` CLI for its
success line):

| field | required | notes |
|---|---|---|
| `id` | yes | plugin identifier |
| `version` | yes | plugin version |
| `entrypoint` | yes | `"module:Class"` — must contain a `:` |
| `triggers` | yes | array of event types; at least one |
| `permissions` | no | array; every entry must be in the permission vocabulary below |
| `timeout_seconds` | no | positive integer (default 60) |
| `name` | no | display name (falls back to `id`) |
| `configuration` | no | array of `[[configuration]]` parameter tables |

Other keys you may see in real manifests (`description`, `capabilities`, `sdk`,
`runtime`, …) are consumed elsewhere in the platform; this SDK's validator
neither requires nor rejects them.

### Permissions

The vocabulary is a **closed set** (`manifest.PERMISSIONS`) — a manifest that
requests anything else is a hard validation error:

```
read:case            write:case
read:alert           write:task
read:observable      write:observable
                     write:observable_enrichment
                     write:plugin_result
```

### `[[configuration]]` parameters

Each parameter table may declare: `name` (required), `type` (required),
`choices` (array), `min`/`max` (numbers), `defaultValue` (or `default`),
`secret` (bool). Example, from the AbuseIPDB reference plugin:

```toml
[[configuration]]
name = "key"
type = "string"
secret = true              # marks this a secret → lands in ctx.secrets, no default
required = true
description = "AbuseIPDB API key"

[[configuration]]
name = "days"
type = "integer"
required = false
defaultValue = 30          # non-secret default → lands in ctx.config
description = "Max age of reports to consider, in days"
```

**Secrets — two accepted conventions.** A parameter is treated as secret if it
has `secret = true` **or** `type = "secret"`. Either way its value arrives in
`ctx.secrets`, never `ctx.config`, and it never carries a default.

**Defaults — `defaultValue`, with `default` as a fallback.** The real
convention in `catlico-plugin.toml` is `defaultValue`. The loader
(`manifest_defaults`) reads `defaultValue` first and falls back to `default` if
`defaultValue` is absent, matching the server's precedence
(`catlico-api/app/api/v1/routes/plugins.py`,
`catlico-web/.../buildForm.ts`). Prefer `defaultValue`.

**Config `type` is not a closed vocabulary.** The validator *recognises*
`string`, `integer`, `float`, `boolean`, `array`, `cron`, `secret`. An
unrecognised `type` produces a **warning, not an error**, and never fails
validation or a run — because production treats `type` as freeform (neither the
Catlico API nor the runner installer rejects an unknown type). The warning just
flags a likely typo.

---

## Testing your plugin

`catlico_plugin_sdk.testing` gives you a fake runtime with no live Catlico API
and no network. It is **not** re-exported from the package top level — import it
directly:

```python
from catlico_plugin_sdk.testing import FakeContext, observable_event, fake_http
```

The centerpiece is **`FakeContext`** — a real `PluginContext` subclass backed by
**`FakeCatlicoApi`**. Its key property: it enforces the *same* manifest
permissions production enforces. Pass your manifest and permissions are derived
from `manifest["permissions"]`; a call your manifest didn't declare raises
`PermissionDenied` (the offline equivalent of the runtime's 403).

What `FakeContext` records, for assertions:

- **`ctx.results`** — enrichments / results / proposed actions the plugin emitted.
- **`ctx.uploaded_files`** — everything passed to `upload_file`.
- **`ctx.progress_updates`** — every `progress(...)` call.
- **`ctx.permission_denials`** — every attempted over-reach, and
  **`ctx.assert_no_permission_violations()`** fails the test if the list is
  non-empty.

Seed reads with `cases=`, `alerts=`, `observables=`, `downloads=` (an unseeded
read raises `LookupError`, mirroring a 404). Supply `config=` / `secrets=`
directly.

Event factories build valid envelopes: **`observable_event(...)`**,
**`case_event(...)`**, **`alert_event(...)`** (and `make_envelope(...)` for the
raw dict). **`fake_http(handler)`** returns a `PluginHttp` whose requests are
served by your handler — return a `403` to exercise `ConfigError`, a `500`/`429`
to exercise `TransientError`.

### Example (adapted from `tests/test_testing.py`)

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
    await MyPlugin().process(observable_event(data="1.2.3.4"), ctx)

    assert ctx.results[0]["verdict"] == "malicious"
    assert ctx.progress_updates == [{"message": "checking", "percent": 50}]
    ctx.assert_no_permission_violations()
    await ctx.http.aclose()
```

If `MyPlugin` tried, say, `ctx.api.get_case(1)` without `read:case` in the
manifest, that call raises `PermissionDenied` and the test fails — exactly as the
runtime would reject it. (Tests are `async`; this repo runs them under
`pytest-asyncio` in `asyncio_mode = "auto"`, so no `@mark.asyncio` needed.)

---

## The CLI — `catlico-plugin`

Installed as the `catlico-plugin` script (entry point
`catlico_plugin_sdk.cli:main`). `validate` runs fully offline. `run` fakes only
the Catlico API (`ctx.api`) and never touches a live runner — but it wires a
**real** `PluginHttp`, so your plugin's `ctx.http` vendor calls do go out over
the network (see below).

### `catlico-plugin validate [PATH]`

Validates a plugin's `catlico-plugin.toml` (`PATH` is a plugin directory or a
manifest file; default `.`).

```
$ catlico-plugin validate ./my-plugin
ok: My Plugin 0.1.0 manifest is valid
```

Warnings are printed but do **not** fail validation; **exit code 0** when the
manifest is valid even if warnings were emitted. **Exit code 1** on a hard error:
a missing manifest, unparseable TOML, or any validation error.

### `catlico-plugin run --event EVENT.json [PATH] [--config C.json] [--secrets S.json]`

Imports the plugin via its manifest entrypoint, parses `EVENT.json` into a
`PluginEvent`, and drives `should_process` → `process` against a `FakeContext`,
then prints emitted results, progress, uploaded files, and any permission
violations. `--config` / `--secrets` point at JSON fixtures; manifest
`defaultValue`s are applied first and `--config` overrides them. (Unlike the test
kit's fake, `run` wires a *real* `PluginHttp`, so vendor calls actually go out —
provide `--secrets` for real credentials, or point the plugin at a mock server.)

```
$ catlico-plugin run ./my-plugin --event event.json --secrets secrets.json
status: success

results (1):
  - {"kind": "enrichment", "source": "demo", ...}

progress (1):
  - looking up 1.2.3.4 (50%)
```

**Exit codes.** `0` when the run ends `success` **or** `skipped`
(`should_process` returned `False`). `1` on a hard error before the plugin runs
(missing/unparseable/invalid manifest, unreadable event, bad config/secrets
fixture, import failure) **or** when the plugin run ends in `failure`. A manifest
*warning* is printed but never aborts a run.

---

## Development

```bash
uv run pytest tests -q      # run the SDK's own test suite
```

Python **3.14+**. Dependencies are declared in `pyproject.toml` (`httpx`,
`pydantic`; `pytest` + `pytest-asyncio` for the dev group).

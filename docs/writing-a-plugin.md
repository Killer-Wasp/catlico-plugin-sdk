# Writing a plugin

The authoring contract: the base class, the event, the runtime context, and error handling.

## Where a plugin runs

A Catlico plugin is a small Python package that reacts to Catlico events (an observable was
created, a case was opened, …) and writes back *evidence* — enrichments, results, proposed
case edits. You write it against this SDK; at runtime the
[plugin runner](https://github.com/Killer-Wasp/catlico-plugin-runner) loads your package and
executes it in an isolated sandbox (a subprocess or container — `catlico_plugin_sdk._worker`
is the sandbox entrypoint).

Two boundaries matter, and the SDK enforces both:

- **Plugin code never touches the database and never calls the public `/api/v1` surface.**
  All reads and writes go through `ctx.api`, which speaks to Catlico's *internal*
  plugin-runtime API using a short-lived, run-scoped token minted for that one run.
- **A plugin can only do what its manifest declares.** The run token carries the permissions
  from your `catlico-plugin.toml`; the runtime rejects any call outside them (HTTP 403). The
  test kit enforces the *same* rules offline, so an over-reach fails in your unit tests, not
  in production.

## The base class

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

Base-class defaults (`plugin.py`): `health` returns `{"ok": True}`, `should_process` returns
`event.event_type in self.triggers`, and `process` raises `NotImplementedError` — you must
override `process`.

## The event

`process` and `should_process` receive a `PluginEvent` — a thin, already-parsed envelope.
Filter on its fields cheaply; fetch full entity detail through `ctx.api` only when you need it.

| Attribute | Meaning |
|---|---|
| `event_id` | audit id of the event |
| `event_type` | e.g. `"observable.created"` |
| `organisation_id` | tenant the event belongs to |
| `object_type` / `object_id` | the entity the event is about (`"observable"`, its id) |
| `data` | event payload dict (e.g. `{"observable_type": "ip", "data": "1.2.3.4"}`) |
| `occurred_at`, `actor`, `context`, `request_id`, `attempt` | envelope metadata |

`attempt` starts at 1 and increases on retries — useful if a step must be idempotent.

## The runtime context (`ctx`)

Every hook receives a context. In production it is a `PluginContext`; in tests a `FakeContext`
(a subclass, so a real drop-in).

- **`ctx.config`** — dict of non-secret configuration (manifest `defaultValue`s merged with
  org-level overrides)
- **`ctx.secrets`** — dict of secret values. Kept separate from `config`; secret parameters
  never carry a default
- **`ctx.permissions`** — the set of permission strings granted this run;
  `ctx.has_permission(name)` tests one
- **`ctx.progress(message, percent=None)`** — best-effort progress reporting
- **`ctx.api`** / **`ctx.http`** — the two clients, below
- Run metadata: `ctx.run_id`, `ctx.plugin_id`, `ctx.plugin_version`, `ctx.organisation_id`,
  `ctx.event_id`

### `ctx.api` — reading and writing Catlico entities

All methods are `async`. The permission each requires — enforced by the runtime *and* by the
test fake — is shown alongside.

| Method | Permission |
|---|---|
| `get_observable(observable_id: str) -> dict` | `read:observable` |
| `get_case(case_id: int) -> dict` | `read:case` |
| `get_alert(alert_id: int) -> dict` | `read:alert` |
| `add_result(**body) -> dict` | `write:plugin_result` **or** `write:observable_enrichment` |
| `add_observable_enrichment(observable_id, *, source, data, **body) -> dict` | `write:observable_enrichment` |
| `propose_case_patch(case_id: int, **fields) -> dict` | `write:case` |
| `propose_task(case_id: int, **fields) -> dict` | `write:task` |
| `propose_tag(case_id: int, tag: str) -> dict` | `write:case` |
| `progress(message, percent=None) -> dict` | *(none)* |
| `upload_file(content, filename, content_type=…) -> dict` | `write:plugin_result` **or** `write:observable_enrichment` |
| `download_file(file_ref: str) -> bytes` | *(none — scoped by the run token)* |

`add_result` requires `entity_type`, `entity_id`, and a truthy `fingerprint`; the runtime
**dedups on `fingerprint` per run** (a repeat returns the same id with `created: False`).

The `propose_*` methods create **proposed actions** that an analyst must approve — they do not
mutate canonical data directly. A plugin cannot silently change a case.

### `ctx.http` — outbound vendor calls with automatic error classification

`ctx.http` is a thin `httpx.AsyncClient` wrapper (`request`, `get`, `get_json`, `post`) that
turns transport and HTTP failures into the SDK's typed exceptions, so retry semantics come for
free:

| Condition | Raises |
|---|---|
| status `401` or `403` | `ConfigError` |
| status `429` or `>= 500` | `TransientError` |
| timeout (`httpx.TimeoutException`) | `TransientError` |
| other transport error | `TransientError` |

Any other status — including 2xx and, notably, other 4xx like 400/404 — is returned to you as
a normal `httpx.Response`. Inspect `resp.status_code` yourself and raise if you want to fail.

`ctx.http` does **not** enforce plugin permissions; those apply only to `ctx.api`.

## Error handling

Raise one of these from `health`/`process` to fail a run. Each carries an `error_kind` the
runner uses to classify the failure and decide whether to retry:

| Exception | `error_kind` | Retried? | Meaning |
|---|---|---|---|
| `TransientError` | `transient` | yes | temporary upstream failure (vendor 429/5xx, network) |
| `ConfigError` | `config` | no | config/credential invalid; trips a circuit breaker after repeated failures |
| `InputError` | `input` | no | the event/entity is unsupported or malformed |
| `PluginRuntimeError` (base) | `bug` | no | anything else you raise deliberately |

Any *other* uncaught exception is treated as a `bug` too. Don't wrap ordinary bugs in these
exceptions; let them surface. Because `ctx.http` already maps `401/403 → ConfigError` and
`429/5xx/network → TransientError`, a plugin that calls vendors through `ctx.http` gets
sensible retry behaviour without writing any of this by hand.

All four exceptions are importable from the package top level
(`from catlico_plugin_sdk import ConfigError`).

## What not to do

- **Never print or log secrets.** The runner redacts run-secret values from the log tail,
  but only as a backstop — base64/URL-encoded or otherwise transformed secrets pass through
  unredacted.
- **No vendor or API calls in `should_process`.** It is a cheap envelope filter; the runtime
  budget assumes it costs nothing.
- **Don't use a bare `httpx`/`requests` client** — you lose the error classification and the
  run misclassifies failures.

## See also

- [manifest.md](manifest.md) — the `catlico-plugin.toml` schema and permission vocabulary
- [testing.md](testing.md) — the `FakeContext` test kit
- [cli.md](cli.md) — `catlico-plugin validate` and `run`

# Writing a plugin

The authoring contract: the app and its decorators, the event, the runtime context, and error
handling.

## Where a plugin runs

A Catlico plugin is a small uv project that reacts to Catlico events (an observable was
created, a case was opened, …) and writes back *evidence* — enrichments, results, proposed
case edits. You write it against this SDK; at runtime the
[plugin runner](https://github.com/Killer-Wasp/catlico-plugin-runner) provisions it as a
per-plugin uv project with its own dependency venv and executes each run in a plain child
process bound to that venv's interpreter (`catlico_plugin_sdk._worker` is the entrypoint the
runner spawns). Plugins are trusted first-party code — there is no sandbox; the process runs
with the runner's privileges.

Two boundaries matter, and the SDK enforces both:

- **Plugin code never touches the database and never calls the public `/api/v1` surface.**
  All reads and writes go through `ctx.api`, which speaks to Catlico's *internal*
  plugin-runtime API using a short-lived, run-scoped token minted for that one run.
- **A plugin can only do what its manifest declares.** The run token carries the permissions
  from your `catlico-plugin.toml`; the runtime rejects any call outside them (HTTP 403). The
  test kit enforces the *same* rules offline, so an over-reach fails in your unit tests, not
  in production.

## The app

A plugin builds one `Catlico` app and decorates plain `async` functions with it. The manifest's
`entrypoint` names that app (`entrypoint = "main:catlico"` — module `main`, object `catlico`).
By convention the app lives in a root `main.py` and delegates to functions in `src/<pkg>/`:

```python
# main.py — the entrypoint the runner imports
from catlico_plugin_sdk import Catlico, ConfigError, SkipRun

catlico = Catlico()


@catlico.event(
    "observable.created",
    matchers=[lambda e: e.data.get("observable_type") == "ip"],  # cheap envelope filter
)
async def enrich(event, ctx):
    # The real work. Raise a PluginRuntimeError subclass to fail the run,
    # or raise SkipRun(reason) to end the run as "skipped".
    ...


@catlico.health()
async def health(ctx):
    # Called at register/enable and on config change. Return a dict, or raise
    # to fail the health check.
    if not ctx.secrets.get("key"):
        raise ConfigError("API key is not configured")
    return {"ok": True}
```

The decorators return the wrapped function **unchanged** (Bolt-style), so each handler is a
normal `async def` you can call directly in a unit test. All handlers are `async`.

- `@catlico.event(event_type, matchers=())` registers a handler. **Multiple handlers may
  register for the same event**; `dispatch` runs them in registration order. `matchers` is a
  list of cheap sync-or-async callables taking `(event)` or `(event, ctx)` — return falsey to
  reject. A handler whose matchers reject is not run.
- `@catlico.health()` registers the health check (at most one).

**The manifest's `triggers` must equal the app's registered events**, exactly. The worker (and
`catlico-plugin run`/`validate`) enforce this strict equality — a trigger with no matching
`@catlico.event` handler, or vice versa, is a config error at load, never a silent no-op.

### Two ways to skip an event

Both classify the run as `skipped` (preserving the API's skipped-run accounting):

- **`matchers=[...]`** on the decorator — cheap envelope filters, evaluated before the handler
  runs. Use for `event.data`/`object_type` checks that cost nothing.
- **`raise SkipRun(reason)`** inside the handler — for a decision that needs work (a lookup, a
  config read) before you know the event is irrelevant.

If several handlers register for one event and at least one completes, the run is `success`;
if every handler was skipped, the run is `skipped`. The first handler to raise a
non-`SkipRun` exception fails the run and later handlers don't run.

## The event

Handlers receive a `PluginEvent` — a thin, already-parsed envelope. Filter on its fields
cheaply (in a matcher); fetch full entity detail through `ctx.api` only when you need it.

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

Raise one of these from a handler (or `health`) to fail a run. Each carries an `error_kind` the
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

All four exceptions — plus `Catlico` and `SkipRun` — are importable from the package top level
(`from catlico_plugin_sdk import ConfigError, SkipRun`).

## What not to do

- **Never print or log secrets.** The runner redacts run-secret values from the log tail,
  but only as a backstop — base64/URL-encoded or otherwise transformed secrets pass through
  unredacted.
- **No vendor or API calls in a `matchers=` filter.** Matchers are cheap envelope checks; the
  runtime budget assumes they cost nothing. Do work-that-decides-to-skip with `SkipRun`.
- **Don't use a bare `httpx`/`requests` client** — you lose the error classification and the
  run misclassifies failures.

## See also

- [manifest.md](manifest.md) — the `catlico-plugin.toml` schema and permission vocabulary
- [testing.md](testing.md) — the `FakeContext` test kit
- [cli.md](cli.md) — `catlico-plugin validate` and `run`

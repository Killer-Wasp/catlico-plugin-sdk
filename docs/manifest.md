# The manifest — `catlico-plugin.toml`

A plugin declares its identity, triggers, permissions, and configurable parameters in
`catlico-plugin.toml` at the plugin root. `manifest.py` is the SDK-side schema authority; the
Catlico runtime and the runner's install validator enforce the same rules.

## Top-level fields

Fields the SDK validator reads (`name` is read by the `validate` CLI for its success line):

| Field | Required | Notes |
|---|---|---|
| `id` | yes | plugin identifier |
| `version` | yes | plugin version |
| `entrypoint` | yes | `"module:Class"` — must contain a `:` |
| `triggers` | yes | array of event types; at least one |
| `permissions` | no | array; every entry must be in the vocabulary below |
| `timeout_seconds` | no | positive integer (default 60) |
| `name` | no | display name (falls back to `id`) |
| `configuration` | no | array of `[[configuration]]` parameter tables |

Other keys you'll see in real manifests (`description`, `capabilities`, `sdk`, `runtime`, …)
are consumed elsewhere in the platform; this validator neither requires nor rejects them.

**Entrypoint resolution.** `entrypoint = "my_plugin.plugin:MyPlugin"` is split on `:` into a
module path and a class name; the loader does `importlib.import_module(module)` then
instantiates the class with no arguments.

## Permissions

The vocabulary is a **closed set** (`manifest.PERMISSIONS`) — a manifest requesting anything
else is a hard validation error, and the platform rejects it before it ships:

```
read:case            write:case
read:alert           write:task
read:observable      write:observable
                     write:observable_enrichment
                     write:plugin_result
```

Request the narrowest set that works. Each `ctx.api` method requires a specific permission —
the full method→permission table is in [writing-a-plugin.md](writing-a-plugin.md#ctxapi--reading-and-writing-catlico-entities).

> **Adding a permission is a three-repo change**: this frozenset, the runner's install
> validator (`_ALLOWED_PERMISSIONS`), and the API's runtime enforcement
> (`catlico-api/app/api/internal/routes/plugin_runtime.py`). Never change one in isolation.

## `[[configuration]]` parameters

Each parameter table may declare: `name` (required), `type` (required), `choices` (array),
`min`/`max` (numbers), `defaultValue` (or `default`), `secret` (bool), `required` (bool),
`description`.

```toml
[[configuration]]
name = "key"
type = "string"
secret = true              # secret → lands in ctx.secrets, never carries a default
required = true
description = "AbuseIPDB API key"

[[configuration]]
name = "days"
type = "integer"
required = false
defaultValue = 30          # non-secret default → lands in ctx.config
description = "Max age of reports to consider, in days"
```

### Secrets — two accepted conventions

A parameter is secret if it has `secret = true` **or** `type = "secret"`. Either way its value
arrives in `ctx.secrets`, never `ctx.config`, and it never carries a default.

### Defaults — `defaultValue`, with `default` as fallback

The convention is **`defaultValue`**. The loader (`manifest_defaults`) reads `defaultValue`
first and falls back to `default`, matching the server's precedence. Prefer `defaultValue`.

### `type` is not a closed vocabulary

The validator *recognises* `string`, `integer`, `float`, `boolean`, `array`, `cron`, `secret`.
An unrecognised `type` produces a **warning, not an error** — production treats `type` as
freeform (neither the Catlico API nor the runner installer rejects an unknown type). The
warning just flags a likely typo.

## Hard errors vs warnings

`validate_manifest` returns hard errors **only** for shapes the platform would reject:

- missing `id`, `version`, or `entrypoint`
- an entrypoint without `:`
- no triggers
- a permission outside the vocabulary
- a non-positive `timeout_seconds`
- structurally malformed `[[configuration]]` tables

Everything else — notably an unrecognised config `type` — comes back from `manifest_warnings`
as advisory. **A warning must never fail a manifest the platform would accept.**

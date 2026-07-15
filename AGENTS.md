# Catlico Plugin SDK

## Collaboration Principles

- Ask, don't assume. If something is unclear, ask before writing a single line. Never make silent assumptions about intent, architecture, or requirements. When running unattended, pick the most reasonable interpretation, proceed, and record the assumption rather than blocking.
- Implement the simplest solution for simple problems, and better solutions for harder problems. Do not over-engineer or add flexibility that is not needed yet.
- Do not touch unrelated code. Surface bad code or design smells you discover so they can be addressed as separate issues.
- Flag uncertainty explicitly. If unsure, ask before proceeding. When useful, conduct a small, localized, low-risk experiment, then bring the hypothesis and results back for discussion. Confidence without certainty causes more damage than admitting a gap.
- Suggest better approaches when they would improve the work, especially when they have a longer-lasting impact than a tactical change.

## What this is

`catlico-plugin-sdk` is the **authoring contract** for Catlico plugins: the `Catlico`
decorator app a plugin builds, the context object injected into its handlers, the manifest
schema, a test kit, and a three-command CLI. It is a library, not a service.

Three parties depend on this package, which is why changes here ripple widely:

- **Plugin authors** import `Catlico`, `SkipRun`, `PluginContext`, and the exception types.
- **`catlico-plugin-runner`** spawns `python -m catlico_plugin_sdk._worker` with each plugin's
  own venv interpreter (per-plugin venv; no sandbox — trusted first-party code).
- **`catlico-api`** enforces, at the internal runtime endpoints, the same permission
  vocabulary this package validates. `manifest.py` is the SDK-side authority; the API
  and the runner's registry validator are the enforcing authorities. **They must agree.**

## Stack

- **Python 3.14+**, **hatchling**, **uv**
- **httpx** for the wrapped outbound HTTP client; **pytest + pytest-asyncio** for tests

## Layout

```
catlico_plugin_sdk/
  app.py        # Catlico app + SkipRun; @event/@health decorators, dispatch/check_health
  plugin.py     # the 4 exception types (error_kind vocabulary)
  models.py     # PluginEvent, PluginContext
  api.py        # PluginApiClient (ctx.api), PluginHttp (ctx.http)
  manifest.py   # manifest schema authority: PERMISSIONS, validate/warnings
  cli.py        # `catlico-plugin new`, `validate`, and `run`
  scaffold.py   # `new` subcommand's plugin-tree generator (id/package derivation + templates)
  testing.py    # FakeContext test kit + run_app — deliberately NOT re-exported
  _worker.py    # worker entrypoint the runner spawns with the plugin's venv python
docs/
  quickstart.md        # 15-minute guided walkthrough (new -> edit -> validate -> run -> test)
  writing-a-plugin.md  # the full authoring contract
  manifest.md           # catlico-plugin.toml schema
  testing.md             # the FakeContext test kit
  cli.md                  # new / validate / run in full
examples/
  defang-annotator/     # a finished, runnable, offline example plugin (see its README)
```

## The authoring contract

Build one `Catlico` app (conventionally in a root `main.py`; `entrypoint = "main:catlico"`) and
decorate plain `async` functions:

- `@catlico.event(event_type, matchers=())` — register an event handler `(event, ctx)`. Multiple
  per event, run in registration order. `matchers` are cheap sync/async envelope filters
  `(event)` or `(event, ctx)`; reject → the handler is skipped. Returns the fn unchanged.
- `@catlico.health()` — register the health check `(ctx) -> dict` (at most one).
- `raise SkipRun(reason)` inside a handler to skip; the exception classes below to fail.

`dispatch` runs matching handlers and classifies the outcome (success/skipped/failure). The
manifest's `triggers` MUST equal the app's `registered_events` — the worker and CLI enforce
strict equality. The runner spawns the worker with the plugin's own venv python; the
`_worker` module never adds runner site-packages to the path (StackStorm isolation lesson).

`PluginContext` carries `run_id`, `plugin_id`, `plugin_version`, `organisation_id`,
`event_id`, `permissions`, `config`, `secrets`, plus `ctx.api` and `ctx.http`.
Use `ctx.has_permission(...)` and `await ctx.progress(msg, percent)`.

### Two clients, two different rulesets — do not confuse them

- **`ctx.api`** (`PluginApiClient`) talks to the Catlico internal API and **is
  permission-checked**. Reads: `get_observable`, `get_case`, `get_alert`. Writes:
  `add_result`, `add_observable_enrichment`, `upload_file`, `download_file`.
  Mutations to canonical entities go through `propose_case_patch`, `propose_task`,
  and `propose_tag` — these create `PluginProposedAction` rows for human approval
  rather than mutating directly. `progress` and `download_file` need no permission.
- **`ctx.http`** (`PluginHttp`) is for **outbound vendor calls** and enforces **no
  plugin permissions**. It classifies failures: `401/403 → ConfigError`;
  `429` and `>=500` → `TransientError`; timeouts/transport errors → `TransientError`.
  **Other 4xx (400, 404) pass through as an ordinary `Response`** — the plugin must
  handle them.

### Exceptions map to `error_kind`

Each exception sets the `error_kind` that classifies the run and decides retryability:

| Exception | `error_kind` |
|---|---|
| `PluginRuntimeError` (base) | `bug` |
| `ConfigError` | `config` |
| `TransientError` | `transient` |
| `InputError` | `input` |

## The manifest is a shared vocabulary

`manifest.py` owns the schema for `catlico-plugin.toml`. The permission set is a
**closed vocabulary of exactly eight** strings:

```
read:case   read:alert   read:observable
write:case  write:task   write:observable  write:observable_enrichment  write:plugin_result
```

Adding a permission here is a **three-repo change**: this frozenset, the runner's
registry validator (`_ALLOWED_PERMISSIONS` in `plugin_runner/registry.py`), and the API's
runtime enforcement in `app/api/internal/routes/plugin_runtime.py`. Never add one in isolation.

**Hard errors** (a manifest the platform would reject): missing required top-level
fields, malformed `module:app_object` entrypoint, no triggers, an unknown permission,
non-positive `timeout_seconds`, structurally malformed `[[configuration]]`.
**Warnings** (`manifest_warnings`) are advisory only — notably an unrecognised config
`type`, which production treats as freeform. A warning must never fail a manifest the
platform would accept.

## CLI

Installed as the `catlico-plugin` script. **Three subcommands**: `new`, `validate`, `run`.

- `new <plugin-id> [--dir DIR] [--name NAME]` — scaffolds a fresh plugin tree (root `main.py`
  app + `src/<pkg>/plugin.py` functions) via `catlico_plugin_sdk/scaffold.py`. Exit `0` on
  success, `1` for an invalid id or a non-empty existing target directory. The generated
  manifest round-trips clean through `validate_manifest`/`manifest_warnings` by construction.
- `validate <path>` — fully offline. Exit `0` on clean or warnings-only, `1` on hard errors.
- `run <path>` — exit `0` for `success`/`skipped`, `1` for `failure` or a pre-run hard error.

> **`run` is not offline.** It fakes only the Catlico API (`ctx.api`) and never invokes
> the runner, but it wires a **real `PluginHttp`**, so vendor calls in `ctx.http` go out
> over the live network. Only `validate` is network-free.

## Conventions

- `testing` is **not** re-exported from `__init__.py`. Import it explicitly as
  `from catlico_plugin_sdk import testing`. The public surface is `__all__`
  (`Catlico`, `SkipRun`, `PluginContext`, `PluginEvent`, `PluginApiClient`,
  `PluginHttp`, the four exceptions, and `__version__`).
- Config parameters use `defaultValue` (not `default`) in the manifest. Secrets are
  declared with `secret = true` or `type = "secret"`; the API stores them and the
  runner injects them per-run.
- `_worker.py` inserts the `plugin_path` it is *given* (the plugin ROOT, where `main.py` lives)
  onto `sys.path`; the plugin's `src/` package comes from the venv (uv installs the project), so
  the worker never computes `<plugin>/src`. It NEVER adds runner site-packages to the path.
- Keep secrets out of reports, logs, and test fixtures. The runner redacts run-secret
  values from the log tail as a **backstop only** — encoded or transformed secrets pass
  through unredacted.

## Testing

`testing.FakeContext` records `ctx.results` and `ctx.progress_updates`, fakes `ctx.api`,
and offers `assert_no_permission_violations()` so a test fails when a plugin calls an
API method its manifest didn't request. Close the real `ctx.http` in teardown
(`await ctx.http.aclose()`).

```bash
uv sync
uv run pytest
```

## Related

- `catlico-plugin-runner/AGENTS.md` — the sandbox that executes this contract
- `catlico-plugins/AGENTS.md` — the plugin authoring guide and reference plugin
- `docs/quickstart.md` — the 15-minute guided walkthrough; `docs/` also has the full reference:
  `writing-a-plugin.md`, `manifest.md`, `testing.md`, `cli.md`
- `examples/defang-annotator/` — a finished, runnable, fully offline example plugin, generated
  via `catlico-plugin new` and referenced by the quickstart

When documentation and code disagree, treat the code and tests as the source of truth,
then update the stale doc.

# The CLI — `catlico-plugin`

Installed as the `catlico-plugin` script (entry point `catlico_plugin_sdk.cli:main`). Three
subcommands: `new`, `validate`, and `run` — all offline (no live Catlico API, no runner),
though `run` wires a real HTTP client for vendor calls (see below).

> **Network behaviour differs across subcommands.** `new` and `validate` are fully offline.
> `run` fakes the Catlico API (`ctx.api`) and never touches a runner, **but it wires a real
> `PluginHttp`** — your plugin's `ctx.http` vendor calls go out over the live network.

## `catlico-plugin new PLUGIN_ID [--dir DIR] [--name NAME] [--class CLASS]`

Scaffolds a fresh, valid plugin directory tree at `DIR/PLUGIN_ID/` (default `DIR` is the
current directory) — the same shape as `catlico-plugins/abuseipdb`: `catlico-plugin.toml`,
`pyproject.toml`, `Dockerfile.catlico`, `src/<pkg>/{__init__.py,plugin.py}`, a starter
`tests/test_plugin.py`, and a `.github/workflows/ci.yml` that runs `catlico-plugin validate`,
`ruff`, and `pytest` on every push and pull request. Refuses to run if the target directory
already exists and is non-empty.

```
$ catlico-plugin new my-cool-plugin
created ./my-cool-plugin
next steps:
  catlico-plugin validate ./my-cool-plugin
  catlico-plugin run ./my-cool-plugin --event event.json  # see docs/cli.md for the envelope shape
```

The Python package name is derived from `PLUGIN_ID` (hyphens/invalid identifier characters
become underscores, then `_plugin` is appended): `my-cool-plugin` → `my_cool_plugin_plugin`,
matching `abuseipdb` → `abuseipdb_plugin`. The plugin class name defaults to a CamelCase
derivation of the id (`my-cool-plugin` → `MyCoolPluginPlugin`) — override with `--class`.
`--name` sets the manifest's display `name` (default: the plugin id).

The generated manifest passes `validate_manifest` with no errors and `manifest_warnings` with
no warnings out of the box; the generated `plugin.py` is a minimal, runnable `CatlicoPlugin`
subclass with clear `TODO`s where you plug in real logic.

> **Where to run it.** The generated `pyproject.toml` pins the SDK at
> `../../catlico-plugin-sdk`, so scaffold from inside `catlico-plugins/` (or another directory
> two levels below the workspace root, next to `catlico-plugin-sdk`) for that path dependency
> to resolve. `--class` must be a valid Python identifier (and not a reserved keyword).

| Exit code | When |
|---|---|
| `0` | plugin tree created |
| `1` | invalid plugin id, or the target directory exists and is non-empty |

## `catlico-plugin validate [PATH]`

Validates a plugin's `catlico-plugin.toml`. `PATH` is a plugin directory or a manifest file;
default `.`.

```
$ catlico-plugin validate ./my-plugin
ok: My Plugin 0.1.0 manifest is valid
```

Warnings are printed but do **not** fail validation.

| Exit code | When |
|---|---|
| `0` | manifest valid — even if warnings were emitted |
| `1` | hard error: missing manifest, unparseable TOML, or any validation error |

See [manifest.md](manifest.md) for what counts as a hard error versus a warning.

## `catlico-plugin run --event EVENT.json [PATH] [--config C.json] [--secrets S.json]`

Imports the plugin via its manifest entrypoint, parses `EVENT.json` into a `PluginEvent`, and
drives `should_process` → `process` against a `FakeContext`, then prints emitted results,
progress, uploaded files, and any permission violations.

Config starts from the manifest's `defaultValue`s and is overlaid with `--config`; `--secrets`
supplies secrets. Both fixtures are optional JSON files.

```
$ catlico-plugin run ./my-plugin --event event.json --secrets secrets.json
status: success

results (1):
  - {"kind": "enrichment", "source": "demo", ...}

progress (1):
  - looking up 1.2.3.4 (50%)
```

A realistic `event.json` (the envelope shape the runner delivers — see
`PluginEvent.from_envelope`; it must have `event_type` and an `object`):

```json
{
  "event_id": "audit:1",
  "event_type": "observable.created",
  "organisation_id": "org-a",
  "object": { "type": "observable", "id": "obs-1" },
  "data": { "observable_type": "ip", "data": "1.2.3.4", "tlp": 2, "pap": 2 }
}
```

`config.json` → `{ "days": 30 }` · `secrets.json` → `{ "key": "YOUR_KEY" }`

> **`run` makes real vendor HTTP calls.** Only `ctx.api` is faked. Provide a live key via
> `--secrets` for a real round-trip, or point the plugin at a mock server. Expect to spend
> real API quota.

| Exit code | When |
|---|---|
| `0` | run ended `success`, or `skipped` (`should_process` returned `False`) |
| `1` | hard error before the plugin ran (missing/invalid manifest, unreadable event, bad fixture, import failure), or the run ended `failure` |

A manifest *warning* is printed but never aborts a run.

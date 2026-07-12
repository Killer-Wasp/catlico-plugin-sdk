# Catlico Plugin SDK

**The authoring kit for [Catlico](https://github.com/jimmyruann/catlico-backend) plugins —
base class, runtime context, manifest validator, offline test kit, and CLI.**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/)

A Catlico plugin is a small Python package that reacts to platform events — an observable was
created, a case was opened — and writes back evidence: enrichments, results, proposed case
edits. You write it against this SDK; the
[plugin runner](https://github.com/Killer-Wasp/catlico-plugin-runner) executes it in an
isolated sandbox.

```python
from catlico_plugin_sdk import CatlicoPlugin, InputError


class MyPlugin(CatlicoPlugin):
    triggers = ["observable.created"]

    async def process(self, event, ctx) -> None:
        ip = (event.data.get("data") or "").strip()
        if not ip:
            raise InputError("observable has no IP value")

        resp = await ctx.http.get(                        # vendor call — errors auto-classified
            "https://vendor.example/check", params={"ip": ip},
            headers={"Key": ctx.secrets["key"]},
        )
        score = resp.json()["score"]

        await ctx.api.add_observable_enrichment(          # evidence back into Catlico
            event.object_id,
            source="MyVendor",
            data={"score": score},
            verdict="malicious" if score >= 75 else "safe",
            summary=f"score {score}",
        )
```

## Two boundaries, both enforced

- **Plugins never touch the database or the public API.** Every read and write goes through
  `ctx.api`, which speaks to Catlico's internal runtime API with a short-lived, run-scoped token.
- **A plugin can only do what its manifest declares.** The run token carries the permissions
  from `catlico-plugin.toml`; the runtime rejects anything else with a 403 — and the test kit
  enforces the *same* rules offline, so an over-reach fails in your unit tests, not in production.

Canonical edits (patch a case, add a tag, create a task) are **proposals** an analyst
approves — a plugin cannot silently change a case.

## Install

Requires **Python 3.14+**. Runtime dependencies: `httpx`, `pydantic`.

Inside the Catlico workspace, plugins consume the SDK as a path dependency:

```toml
dependencies = ["catlico-plugin-sdk"]

[tool.uv.sources]
catlico-plugin-sdk = { path = "../../catlico-plugin-sdk" }
```

## The dev loop

```bash
catlico-plugin new my-plugin                              # scaffold a fresh plugin tree
catlico-plugin validate ./my-plugin                       # offline manifest check
catlico-plugin run ./my-plugin --event event.json         # drive a run locally
uv run pytest                                             # your tests, with the offline fake
```

> `new` and `validate` are fully offline. `run` fakes the Catlico API but wires a **real**
> HTTP client — vendor calls go out over the live network. See [docs/cli.md](docs/cli.md).

## Documentation

| Doc | What's in it |
|---|---|
| [Writing a plugin](docs/writing-a-plugin.md) | The base class, the event, `ctx.api` / `ctx.http`, error handling |
| [The manifest](docs/manifest.md) | `catlico-plugin.toml` schema, the permission vocabulary, config parameters |
| [Testing](docs/testing.md) | `FakeContext`, event factories, `fake_http`, permission assertions |
| [The CLI](docs/cli.md) | `new`, `validate`, and `run`, exit codes, event fixtures |

Contributors and AI agents: [`AGENTS.md`](AGENTS.md).

## Error model at a glance

| Exception | `error_kind` | Retried? |
|---|---|---|
| `TransientError` | `transient` | yes |
| `ConfigError` | `config` | no — trips a circuit breaker on repeat |
| `InputError` | `input` | no |
| `PluginRuntimeError` (and any uncaught exception) | `bug` | no |

`ctx.http` classifies vendor failures for you: `401/403 → ConfigError`,
`429`/`5xx`/timeouts → `TransientError`. Other 4xx come back as normal responses.

## Related repositories

| Repo | Role |
|---|---|
| [catlico-plugins](https://github.com/Killer-Wasp/catlico-plugins) | The plugin catalog — copy `abuseipdb/` to start a new plugin |
| [catlico-plugin-runner](https://github.com/Killer-Wasp/catlico-plugin-runner) | The sandbox that executes plugins |
| [catlico-backend](https://github.com/jimmyruann/catlico-backend) | The API — permission gate and audit writer |

## Development (of the SDK itself)

```bash
uv sync
uv run pytest
```

The permission vocabulary in `manifest.py` is mirrored by the runner's install validator and
the API's runtime enforcement. **Changing it is a three-repo change.**

## License

[GNU Affero General Public License v3.0](LICENSE).

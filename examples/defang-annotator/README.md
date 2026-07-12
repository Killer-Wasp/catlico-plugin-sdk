# Defang Annotator (example plugin)

A small, self-contained example plugin for the Catlico Plugin SDK. It reacts to
`observable.created` for `ip` / `domain` / `url` / `hostname` observables and writes back
a **defanged** form — safe to paste into a ticket or chat without it becoming a live link
(`1.2.3.4` → `1[.]2[.]3[.]4`, `https://evil.example` → `hxxps[://]evil[.]example`) — plus, for
IP observables, a note when the address falls in a loopback/link-local/private/reserved/
multicast range.

It is **fully offline**: no vendor call (`ctx.http` unused), no secrets. That makes it a good
first read — it isolates the `ctx.api` / config / `ctx.progress` / error-handling half of the
contract without a network dependency. For a plugin that also uses `ctx.http` and a secret, see
[`catlico-plugins/abuseipdb`](https://github.com/Killer-Wasp/catlico-plugins/tree/main/abuseipdb).

This plugin was generated with `catlico-plugin new defang-annotator --dir examples` (see
[../../docs/quickstart.md](../../docs/quickstart.md) for the full walkthrough) and then filled
in — it's the finished artifact that tutorial builds toward.

## What it demonstrates

| SDK feature | Where |
|---|---|
| Reading the event | `event.data.get("data")`, `event.data.get("observable_type")` in `process()` |
| `ctx.api` | `ctx.api.add_observable_enrichment(...)` writes the defanged value back |
| A config value | `ctx.config.get("style")` and `ctx.config.get("annotate_private_ranges")` |
| `ctx.progress` | two progress calls in `process()` |
| Error model | `InputError` for an empty observable value, `ConfigError` for an invalid `style` override (checked in both `health()` and `process()`) |
| `should_process` filtering | skips observable types this plugin doesn't know how to defang |

## Layout

```
catlico-plugin.toml                            # manifest: triggers, permissions, config
pyproject.toml                                  # note the [tool.uv.sources] path — see below
src/defang_annotator_plugin/plugin.py           # the plugin
tests/test_plugin.py                            # FakeContext-based tests
example-event.json                              # a sample observable.created envelope
```

### A note on `[tool.uv.sources]`

`catlico-plugin new` normally scaffolds plugins under `catlico-plugins/`, two directories below
a workspace root that has `catlico-plugin-sdk/` as a sibling — so the generated
`pyproject.toml` pins `catlico-plugin-sdk = { path = "../../catlico-plugin-sdk" }`. This example
instead ships *inside* the SDK repo, at `catlico-plugin-sdk/examples/defang-annotator/` — also
two directories deep, but the two dirs up (`../..`) land directly on the SDK repo root, not on
a sibling directory named `catlico-plugin-sdk`. So this plugin's `pyproject.toml` uses
`catlico-plugin-sdk = { path = "../.." }` instead of the scaffold's default. If you copy this
example elsewhere to start a real plugin, switch back to the scaffold's default (or `new` a
fresh one and adjust the path for wherever it actually sits).

## Validate

```bash
uv run catlico-plugin validate examples/defang-annotator
```

Expect `ok: Defang Annotator 0.1.0 manifest is valid` — 0 errors, 0 warnings.

## Run it locally

```bash
uv run catlico-plugin run examples/defang-annotator --event examples/defang-annotator/example-event.json
```

The sample event is a private IP (`192.168.1.1`), so the printed result includes both the
defanged value and a "private (RFC1918/RFC4193) address" note. No network call is made and no
`--secrets` fixture is needed. Try config overrides:

```bash
echo '{"style": "hxxp"}' > /tmp/config.json
uv run catlico-plugin run examples/defang-annotator \
  --event examples/defang-annotator/example-event.json --config /tmp/config.json
```

## Test

```bash
cd examples/defang-annotator && uv run pytest
```

Run these tests explicitly — a bare `uv run pytest` at the SDK repo root does **not** pick them
up (the root suite's `testpaths` is scoped to `tests/`, and each example plugin has its own
`src/` on pythonpath). From the repo root, point pytest at this directory instead:

```bash
uv run pytest examples/defang-annotator/tests
```

Tests use `catlico_plugin_sdk.testing.FakeContext` and `observable_event`; no network, no live
Catlico API.

## See also

- [../../docs/quickstart.md](../../docs/quickstart.md) — build a plugin like this one from scratch
- [../../docs/writing-a-plugin.md](../../docs/writing-a-plugin.md) — the full authoring contract
- [../../docs/manifest.md](../../docs/manifest.md) — the manifest schema
- [../../docs/testing.md](../../docs/testing.md) — the `FakeContext` test kit
- [../../docs/cli.md](../../docs/cli.md) — `new` / `validate` / `run`

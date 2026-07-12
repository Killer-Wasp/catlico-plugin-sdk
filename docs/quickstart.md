# Quickstart: your first plugin in 15 minutes

A timed, copy-paste-able walkthrough from an empty directory to a working, tested plugin. It
builds a smaller version of [`examples/defang-annotator`](../examples/defang-annotator) — read
that directory afterward as the finished artifact, or diff against it if you get stuck.

This is a guided tour, not the reference. For the full authoring contract, the manifest schema,
the test kit, and the CLI, see [writing-a-plugin.md](writing-a-plugin.md),
[manifest.md](manifest.md), [testing.md](testing.md), and [cli.md](cli.md) — this doc links out
to them rather than repeating them.

Prerequisites: **Python 3.14+** and **[uv](https://docs.astral.sh/uv/)**. No Docker, no API
key, no live Catlico instance — everything here runs offline.

## 0. Where to run this (1 min)

`catlico-plugin new` scaffolds a `pyproject.toml` that pins the SDK as a path dependency:

```toml
[tool.uv.sources]
catlico-plugin-sdk = { path = "../../catlico-plugin-sdk" }
```

That path assumes your plugin sits **two directories below a workspace root that has
`catlico-plugin-sdk/` as a sibling** — the normal Catlico layout, e.g.
`workspace/catlico-plugins/my-first-plugin/`. If you have the `catlico-plugins` repo checked
out next to `catlico-plugin-sdk`, run the next step from inside it:

```bash
cd catlico-plugins   # sibling of catlico-plugin-sdk
```

If you don't (e.g. you only have this SDK repo checked out), scaffold anywhere and fix the one
path afterward — you'll do exactly this in step 1. Either way you end up with a plugin whose
`pyproject.toml` correctly points at a real `catlico-plugin-sdk` checkout.

## 1. Scaffold (1 min)

```bash
uv run catlico-plugin new my-first-plugin
```

From inside the SDK repo itself (no sibling `catlico-plugins` checkout), run it against a
scratch directory instead and fix the generated path:

```bash
mkdir -p /tmp/catlico-quickstart
uv run catlico-plugin new my-first-plugin --dir /tmp/catlico-quickstart
```

Then in `/tmp/catlico-quickstart/my-first-plugin/pyproject.toml`, point `[tool.uv.sources]` at
this repo directly, e.g. `catlico-plugin-sdk = { path = "/absolute/path/to/catlico-plugin-sdk" }`.
(This is the same fix `examples/defang-annotator/pyproject.toml` makes, with a comment
explaining why — see [its README](../examples/defang-annotator/README.md#a-note-on-tooluvsources).)

Either way, you should see:

```
created ./my-first-plugin
next steps:
  catlico-plugin validate ./my-first-plugin
  catlico-plugin run ./my-first-plugin --event event.json  # see docs/cli.md for the envelope shape
```

## 2. Tour the generated files (3 min)

```
my-first-plugin/
  catlico-plugin.toml           # the manifest: identity, triggers, permissions, config
  pyproject.toml                # the [tool.uv.sources] path from step 0
  Dockerfile.catlico             # how the runner packages this plugin (not needed today)
  src/my_first_plugin_plugin/
    __init__.py
    plugin.py                   # the plugin class
  tests/
    test_plugin.py              # a starter FakeContext test
```

**The manifest** (`catlico-plugin.toml`) declares, among other things:

```toml
triggers = ["observable.created"]
permissions = ["read:observable", "write:observable_enrichment"]

[[configuration]]
name = "threshold"
type = "integer"
required = false
defaultValue = 50
description = "Example numeric parameter. TODO: rename, retype, or remove."
```

`permissions` is a **closed vocabulary of eight strings** — see
[manifest.md](manifest.md#permissions). The run token only carries what's declared here; the
runtime (and the offline test kit) reject anything else. `[[configuration]]` entries become
`ctx.config` (or `ctx.secrets` if `secret = true`) — see
[manifest.md](manifest.md#configuration-parameters).

**The plugin class** (`src/my_first_plugin_plugin/plugin.py`) is a minimal, already-runnable
`CatlicoPlugin` subclass:

```python
class MyFirstPluginPlugin(CatlicoPlugin):
    triggers = ["observable.created"]

    async def health(self, ctx) -> dict:
        return {"ok": True}

    async def should_process(self, event, ctx) -> bool:
        return event.event_type in self.triggers

    async def process(self, event, ctx) -> None:
        value = (event.data.get("data") or "").strip()
        if not value:
            raise InputError("observable has no value")
        await ctx.api.add_observable_enrichment(
            event.object_id, source="my-first-plugin",
            data={"value": value}, verdict="info",
            summary="my-first-plugin" + f" processed {value}",
        )
```

**The two boundaries** — the whole point of the SDK (full detail:
[writing-a-plugin.md](writing-a-plugin.md#two-boundaries-both-enforced)):

- **`ctx.api`** — reads/writes Catlico entities (cases, alerts, observables). **Permission
  checked** against the manifest; a call outside your declared permissions is rejected (403 in
  production, `PermissionDenied` in tests).
- **`ctx.http`** — outbound calls to a vendor/API you're integrating with. **Not**
  permission-checked, but every failure is classified for you: `401`/`403` → `ConfigError`,
  `429`/`5xx`/timeouts → `TransientError`. This quickstart doesn't use it — the finished example
  ([`examples/defang-annotator`](../examples/defang-annotator)) doesn't either, deliberately, so
  it runs with no network. `catlico-plugins/abuseipdb` is the reference for a plugin that does.

## 3. Edit `process()` to do something real (5 min)

Replace the stub in `src/my_first_plugin_plugin/plugin.py` with something that actually reads
the observable, uses a config value, reports progress, and produces a real (if simple) verdict —
mirroring [`examples/defang-annotator`](../examples/defang-annotator/src/defang_annotator_plugin/plugin.py).
Here's a trimmed version — annotate an IP observable with whether it's a private/reserved
address:

```python
from __future__ import annotations

import ipaddress

from catlico_plugin_sdk import CatlicoPlugin
from catlico_plugin_sdk.plugin import InputError


class MyFirstPluginPlugin(CatlicoPlugin):
    triggers = ["observable.created"]

    async def should_process(self, event, ctx) -> bool:
        return (
            event.event_type in self.triggers
            and event.data.get("observable_type") == "ip"
        )

    async def process(self, event, ctx) -> None:
        value = (event.data.get("data") or "").strip()
        if not value:
            raise InputError("observable has no value")

        await ctx.progress(f"checking {value}", percent=50)

        # a config value from catlico-plugin.toml's [[configuration]], e.g. rename
        # `threshold` -> a `flag_private` boolean, or just read what's already there:
        verbose = bool(ctx.config.get("threshold", 50) > 0)

        addr = ipaddress.ip_address(value)
        is_private = addr.is_private or addr.is_loopback or addr.is_link_local
        verdict = "safe" if is_private else "info"
        summary = f"{value} is {'private/reserved' if is_private else 'a public address'}"

        await ctx.api.add_observable_enrichment(
            event.object_id,
            source="my-first-plugin",
            data={"value": value, "is_private": is_private, "verbose": verbose},
            verdict=verdict,
            summary=summary,
        )
```

This is deliberately close to `defang-annotator`'s `private_range_note` helper — worth reading
side by side. Note the error model in play: `InputError` for a malformed/empty event (not
retried), and you'd reach for `ConfigError` if a config value like `style` came back invalid
(see the example's `health()`/`process()` for that pattern) — full table in
[writing-a-plugin.md](writing-a-plugin.md#error-handling).

## 4. Validate (1 min)

```bash
uv run catlico-plugin validate ./my-first-plugin
```

```
ok: my-first-plugin 0.1.0 manifest is valid
```

Fully offline — no network, no live API. See [cli.md](cli.md#catlico-plugin-validate-path) for
what counts as a hard error versus a warning.

## 5. Run it locally (2 min)

Write an event fixture (or copy
[`examples/defang-annotator/example-event.json`](../examples/defang-annotator/example-event.json)):

```bash
cat > event.json <<'EOF'
{
  "event_id": "audit:1",
  "event_type": "observable.created",
  "organisation_id": "org-a",
  "object": { "type": "observable", "id": "obs-1" },
  "data": { "observable_type": "ip", "data": "192.168.1.1", "tlp": 2, "pap": 2 }
}
EOF

uv run catlico-plugin run ./my-first-plugin --event event.json
```

```
status: success

results (1):
  - {"kind": "enrichment", ..., "summary": "192.168.1.1 is private/reserved"}

progress (1):
  - checking 192.168.1.1 (50%)
```

`run` fakes `ctx.api` but wires a **real** `ctx.http` — if your plugin calls a vendor, that call
goes out over the live network. This quickstart's plugin doesn't call one, so `run` here is
fully offline too. Details in [cli.md](cli.md), under `catlico-plugin run`.

## 6. Write and run a test with `FakeContext` (2 min)

Replace `tests/test_plugin.py`:

```python
from catlico_plugin_sdk.testing import FakeContext, observable_event

from my_first_plugin_plugin.plugin import MyFirstPluginPlugin


async def test_flags_a_private_ip():
    ctx = FakeContext(permissions={"read:observable", "write:observable_enrichment"})
    await MyFirstPluginPlugin().process(observable_event(data="192.168.1.1"), ctx)

    assert ctx.results[0]["data"]["is_private"] is True
    assert ctx.results[0]["verdict"] == "safe"
    ctx.assert_no_permission_violations()
```

```bash
cd my-first-plugin && uv run pytest
```

```
1 passed in 0.0Xs
```

`FakeContext` enforces the **same permission rules** production does — derived from your
manifest's `permissions` list — so an over-reach (e.g. calling `ctx.api.get_case(...)` without
`read:case`) fails your test with `PermissionDenied` instead of surfacing as a 403 later. Full
tour: [testing.md](testing.md).

## Where to go next

- [`examples/defang-annotator`](../examples/defang-annotator) — the finished, richer version of
  what you just built: two config parameters, both error kinds, `should_process` filtering by
  observable type, and a fuller test suite. Its README also has the full explanation of the
  `[tool.uv.sources]` path quirk from step 0/1.
- [writing-a-plugin.md](writing-a-plugin.md) — the complete authoring contract
- [manifest.md](manifest.md) — every manifest field and the permission vocabulary
- [testing.md](testing.md) — the full `FakeContext` test kit, including `fake_http` for vendor
  mocking
- [cli.md](cli.md) — `new`, `validate`, `run` in full, including exit codes

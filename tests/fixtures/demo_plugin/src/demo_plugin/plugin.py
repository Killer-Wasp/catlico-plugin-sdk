"""A minimal plugin used by the CLI `run --event` tests.

Plain module-level functions; ``main.py`` wires them to events. The IP-only
filter is a matcher on the event decorator (see ``main.py``); a missing value is
an in-handler ``InputError``.
"""
from catlico_plugin_sdk.plugin import InputError


async def process(event, ctx) -> None:
    value = (event.data.get("data") or "").strip()
    if not value:
        raise InputError("observable has no value")
    await ctx.progress(f"looking up {value}", percent=50)
    greeting = ctx.config.get("greeting", "hi")
    await ctx.api.add_observable_enrichment(
        event.object_id,
        source="demo",
        data={"greeting": f"{greeting} {value}"},
        verdict="info",
    )

"""A minimal plugin used by the CLI `run --event` tests."""
from catlico_plugin_sdk import CatlicoPlugin
from catlico_plugin_sdk.plugin import InputError


class DemoPlugin(CatlicoPlugin):
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
        await ctx.progress(f"looking up {value}", percent=50)
        greeting = ctx.config.get("greeting", "hi")
        await ctx.api.add_observable_enrichment(
            event.object_id,
            source="demo",
            data={"greeting": f"{greeting} {value}"},
            verdict="info",
        )

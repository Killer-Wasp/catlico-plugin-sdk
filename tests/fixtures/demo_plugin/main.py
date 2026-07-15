"""Demo plugin entrypoint (entrypoint = "main:catlico") for the CLI run tests."""
from catlico_plugin_sdk import Catlico

from demo_plugin import plugin

catlico = Catlico()


@catlico.event(
    "observable.created",
    matchers=[lambda e: e.data.get("observable_type") == "ip"],
)
async def on_observable_created(event, ctx):
    await plugin.process(event, ctx)

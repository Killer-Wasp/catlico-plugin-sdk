"""Defang Annotator entrypoint — the app the runner imports (main:catlico).

The event handler runs only for observable types this plugin can defang
(``matchers=[plugin.supported]``); everything else skips cleanly.
"""
from catlico_plugin_sdk import Catlico

from defang_annotator_plugin import plugin

catlico = Catlico()


@catlico.event("observable.created", matchers=[plugin.supported])
async def on_observable_created(event, ctx):
    await plugin.process(event, ctx)


@catlico.health()
async def health(ctx):
    return await plugin.health(ctx)

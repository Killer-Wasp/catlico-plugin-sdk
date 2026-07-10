"""Plugin base class — the authoring contract for Catlico plugins."""


class PluginRuntimeError(Exception):
    """Raised by plugins to fail a run with a message.

    Its ``error_kind`` drives how the run is classified and whether it is
    retried (see the runner's error classification).
    """

    error_kind = "bug"


class ConfigError(PluginRuntimeError):
    """Plugin config/credential is invalid or unusable. Not auto-retried;
    trips the config circuit breaker after repeated failures."""

    error_kind = "config"


class TransientError(PluginRuntimeError):
    """A temporary upstream failure (vendor 429/5xx, network). Auto-retried."""

    error_kind = "transient"


class InputError(PluginRuntimeError):
    """The event/entity is unsupported or malformed. Not auto-retried."""

    error_kind = "input"


class CatlicoPlugin:
    """Base class for Catlico plugins.

    Plugin authors subclass this and override:
    - ``triggers``: class-level list of event types this plugin handles
    - ``health(ctx)``: health check, returns ``{"ok": True}`` or similar
    - ``should_process(event, ctx)``: cheap filter; return False to skip
    - ``process(event, ctx)``: the actual plugin logic

    The runner calls these in order: health (on activation), then for each
    event: should_process → (if True) process.
    """

    #: Event types this plugin wants to receive. Required.
    triggers: list[str] = []

    async def health(self, ctx) -> dict:
        """Health check. Return ``{"ok": True}`` or raise PluginRuntimeError."""
        return {"ok": True}

    async def should_process(self, event, ctx) -> bool:
        """Cheap filter. Return False to skip this event without a failure."""
        return event.event_type in self.triggers

    async def process(self, event, ctx) -> None:
        """Run the plugin logic. Raise PluginRuntimeError to fail the run."""
        raise NotImplementedError("plugins must override process()")

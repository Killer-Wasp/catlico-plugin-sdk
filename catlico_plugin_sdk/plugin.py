"""Plugin error classes — the failure vocabulary for Catlico plugins.

A plugin's authoring surface is the ``Catlico`` app (``catlico_plugin_sdk.app``);
these exceptions are how a handler fails a run with a machine-classified kind.
"""


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

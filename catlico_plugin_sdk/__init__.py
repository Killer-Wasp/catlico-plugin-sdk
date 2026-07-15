"""catlico-plugin SDK: the ``Catlico`` app, event models, and runtime context."""
from catlico_plugin_sdk.api import PluginApiClient, PluginHttp
from catlico_plugin_sdk.app import Catlico, SkipRun
from catlico_plugin_sdk.models import PluginContext, PluginEvent
from catlico_plugin_sdk.plugin import (
    ConfigError,
    InputError,
    PluginRuntimeError,
    TransientError,
)

#: Bundled SDK version. The runner gates each plugin's manifest ``sdk`` range
#: against this so an SDK-API break fails loudly at load, not mid-run.
__version__ = "0.1.0"

__all__ = [
    "Catlico",
    "ConfigError",
    "InputError",
    "PluginApiClient",
    "PluginContext",
    "PluginEvent",
    "PluginHttp",
    "PluginRuntimeError",
    "SkipRun",
    "TransientError",
    "__version__",
]

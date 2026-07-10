"""Event and context models for the plugin SDK."""
from __future__ import annotations

from dataclasses import dataclass, field


class PluginEvent:
    """A Catlico event delivered to a plugin. Cheap filtering via envelope fields;
    plugins fetch full entity details through the runtime API only when needed."""

    def __init__(
        self,
        event_id: str,
        event_type: str,
        organisation_id: str,
        object_type: str,
        object_id: str,
        data: dict,
        *,
        occurred_at: str = "",
        actor: str = "",
        context: dict | None = None,
        request_id: str = "",
        attempt: int = 1,
    ):
        self.event_id = event_id
        self.event_type = event_type
        self.organisation_id = organisation_id
        self.object_type = object_type
        self.object_id = object_id
        self.data = data
        self.occurred_at = occurred_at
        self.actor = actor
        self.context = context or {}
        self.request_id = request_id
        self.attempt = attempt

    @classmethod
    def from_envelope(cls, envelope: dict) -> "PluginEvent":
        """Parse the event envelope delivered by the runner."""
        event_type = envelope.get("event_type")
        if not event_type:
            raise ValueError("envelope missing event_type")
        obj = envelope.get("object")
        if not obj or not isinstance(obj, dict):
            raise ValueError("envelope missing object")
        return cls(
            event_id=envelope.get("event_id", ""),
            event_type=event_type,
            organisation_id=envelope.get("organisation_id", ""),
            object_type=obj.get("type", ""),
            object_id=obj.get("id", ""),
            data=envelope.get("data", {}),
            occurred_at=envelope.get("occurred_at", ""),
            actor=envelope.get("actor", ""),
            context=envelope.get("context"),
            request_id=envelope.get("request_id", ""),
            attempt=envelope.get("attempt", 1),
        )


@dataclass
class PluginContext:
    """Runtime context injected into every plugin call. Carries the run-scoped
    token and an internal API client for reading/mutating entities."""

    run_id: str
    plugin_id: str
    plugin_version: str
    organisation_id: str
    event_id: str
    permissions: set[str] = field(default_factory=set)
    api: object | None = None  # PluginApiClient — set by runner before execution
    http: object | None = None  # PluginHttp — outbound vendor calls
    # ponytail: plain dict for non-secret config; secrets come via env/temp file
    config: dict = field(default_factory=dict)
    secrets: dict = field(default_factory=dict)

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    async def progress(self, message: str, percent: int | None = None) -> None:
        """Report run progress (best-effort; ignored if no API client is wired)."""
        if self.api is not None:
            await self.api.progress(message, percent)

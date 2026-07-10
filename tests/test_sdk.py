"""Tests for the plugin SDK: event models, plugin base class, and runtime context."""
import pytest
from catlico_plugin_sdk import (
    CatlicoPlugin,
    PluginEvent,
    PluginContext,
    PluginRuntimeError,
)


class _TestPlugin(CatlicoPlugin):
    """Minimal plugin for testing the base class contract."""
    triggers = ["observable.created"]

    async def health(self, ctx):
        return {"ok": True}

    async def should_process(self, event, ctx) -> bool:
        return event.object_type == "observable"

    async def process(self, event, ctx) -> None:
        pass


class TestPluginEvent:
    def test_event_from_envelope(self):
        envelope = {
            "event_id": "audit:abc123",
            "event_type": "observable.created",
            "occurred_at": "2026-07-08T00:00:00Z",
            "organisation_id": "org-a",
            "actor": "user-1",
            "object": {"type": "observable", "id": "obs-uuid"},
            "context": {"type": "case", "id": "123"},
            "data": {"observable_type": "ip", "data": "1.2.3.4", "tlp": 2, "pap": 2},
            "request_id": "req-1",
            "attempt": 1,
        }
        event = PluginEvent.from_envelope(envelope)
        assert event.event_id == "audit:abc123"
        assert event.object_type == "observable"
        assert event.object_id == "obs-uuid"
        assert event.data["observable_type"] == "ip"

    def test_event_requires_event_type(self):
        with pytest.raises(ValueError, match="event_type"):
            PluginEvent.from_envelope({"object": {}})

    def test_event_requires_object(self):
        with pytest.raises(ValueError, match="object"):
            PluginEvent.from_envelope({"event_type": "x"})


class TestPluginContext:
    def test_context_scoped_to_org(self):
        ctx = PluginContext(
            run_id="run-1",
            plugin_id="acme",
            plugin_version="1.0.0",
            organisation_id="org-a",
            event_id="audit:1",
            permissions={"read:observable"},
            api=None,  # ponytail: test without real API client
        )
        assert ctx.organisation_id == "org-a"
        assert "read:observable" in ctx.permissions

    def test_context_has_permission(self):
        ctx = PluginContext(
            run_id="r1", plugin_id="p1", plugin_version="1",
            organisation_id="org-a", event_id="e1",
            permissions={"read:observable", "write:case"},
            api=None,
        )
        assert ctx.has_permission("read:observable") is True
        assert ctx.has_permission("write:alerts") is False


class TestPluginBase:
    @pytest.mark.asyncio
    async def test_plugin_health(self):
        plugin = _TestPlugin()
        result = await plugin.health(PluginContext(
            run_id="r1", plugin_id="p1", plugin_version="1",
            organisation_id="org-a", event_id="e1",
            permissions=set(), api=None,
        ))
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_plugin_should_process(self):
        plugin = _TestPlugin()
        event = PluginEvent.from_envelope({
            "event_type": "observable.created",
            "object": {"type": "observable", "id": "x"},
            "organisation_id": "org-a",
            "data": {},
        })
        ctx = PluginContext(
            run_id="r1", plugin_id="p1", plugin_version="1",
            organisation_id="org-a", event_id="e1",
            permissions=set(), api=None,
        )
        assert await plugin.should_process(event, ctx) is True

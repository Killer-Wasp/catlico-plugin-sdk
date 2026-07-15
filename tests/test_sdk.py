"""Tests for the plugin SDK event models and runtime context.

The ``Catlico`` app (the authoring surface) is covered in ``test_app.py``.
"""
import pytest
from catlico_plugin_sdk import PluginContext, PluginEvent


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

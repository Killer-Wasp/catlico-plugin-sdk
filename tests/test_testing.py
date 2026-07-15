"""Tests for the plugin author test kit (``catlico_plugin_sdk.testing``).

Covers the acceptance criteria: a plugin's logic functions can assert on emitted
results, provoke and detect permission failures, use the file helpers, record
progress, and drive error classification — all with no live Catlico API or
runner. The sample logic here is written as plain async functions/objects (the
decorator app itself is covered in ``test_app.py``).
"""
import httpx
import pytest

from catlico_plugin_sdk import ConfigError, TransientError
from catlico_plugin_sdk.plugin import InputError
from catlico_plugin_sdk.testing import (
    FakeCatlicoApi,
    FakeContext,
    PermissionDenied,
    alert_event,
    case_event,
    fake_http,
    observable_event,
)

MANIFEST = {
    "id": "demo",
    "version": "0.1.0",
    "permissions": ["read:observable", "write:observable_enrichment"],
}


class EnrichingPlugin:
    async def should_process(self, event, ctx) -> bool:
        return event.data.get("observable_type") == "ip"

    async def process(self, event, ctx) -> None:
        value = event.data.get("data") or ""
        if not value:
            raise InputError("no value")
        await ctx.progress("checking", percent=50)
        score = (await ctx.http.get_json(f"https://vendor.test/{value}"))["score"]
        await ctx.api.add_observable_enrichment(
            event.object_id,
            source="vendor",
            data={"score": score},
            verdict="malicious" if score >= 75 else "benign",
        )


# --- should_process / process end to end ---


async def test_should_process_filters_by_type():
    ctx = FakeContext(manifest=MANIFEST)
    assert await EnrichingPlugin().should_process(observable_event(), ctx) is True
    assert (
        await EnrichingPlugin().should_process(
            observable_event(observable_type="domain"), ctx
        )
        is False
    )


async def test_process_emits_result_and_progress():
    ctx = FakeContext(
        manifest=MANIFEST,
        http=fake_http(lambda r: httpx.Response(200, json={"score": 90})),
    )
    await EnrichingPlugin().process(observable_event(data="1.2.3.4"), ctx)

    assert len(ctx.results) == 1
    result = ctx.results[0]
    assert result["source"] == "vendor"
    assert result["verdict"] == "malicious"
    assert result["observable_id"] == "obs-1"

    assert ctx.progress_updates == [{"message": "checking", "percent": 50}]
    ctx.assert_no_permission_violations()
    await ctx.http.aclose()


# --- Permission enforcement (calls outside declared manifest permissions) ---


class OverreachingPlugin:
    async def process(self, event, ctx) -> None:
        # Requests read:case, which the manifest never declared.
        await ctx.api.get_case(1)


async def test_call_outside_declared_permissions_fails():
    ctx = FakeContext(manifest=MANIFEST)  # only read:observable + enrichment
    with pytest.raises(PermissionDenied) as exc:
        await OverreachingPlugin().process(observable_event(), ctx)
    assert exc.value.required == frozenset({"read:case"})


async def test_permission_denial_is_recorded_and_asserted():
    ctx = FakeContext(manifest=MANIFEST)
    with pytest.raises(PermissionDenied):
        await ctx.api.propose_tag(1, "malware")
    assert len(ctx.permission_denials) == 1
    with pytest.raises(AssertionError, match="forbidden runtime calls"):
        ctx.assert_no_permission_violations()


async def test_declared_permission_is_allowed():
    ctx = FakeContext(
        manifest={"permissions": ["read:case", "write:plugin_result"]},
        cases={1: {"id": 1, "title": "Boom"}},
    )
    assert (await ctx.api.get_case(1))["title"] == "Boom"
    out = await ctx.api.add_result(
        entity_type="case", entity_id="1", fingerprint="fp-1", verdict="info"
    )
    assert out["created"] is True
    ctx.assert_no_permission_violations()


async def test_add_result_requires_fingerprint():
    ctx = FakeContext(manifest={"permissions": ["write:plugin_result"]})
    with pytest.raises(ValueError, match="fingerprint"):
        await ctx.api.add_result(entity_type="case", entity_id="1")


async def test_add_result_requires_entity_fields():
    ctx = FakeContext(manifest={"permissions": ["write:plugin_result"]})
    with pytest.raises(ValueError, match="entity_type"):
        await ctx.api.add_result(entity_id="1", fingerprint="fp")
    with pytest.raises(ValueError, match="entity_id"):
        await ctx.api.add_result(entity_type="case", fingerprint="fp")


async def test_add_result_dedups_on_fingerprint():
    ctx = FakeContext(manifest={"permissions": ["write:plugin_result"]})
    first = await ctx.api.add_result(
        entity_type="observable", entity_id="o1", fingerprint="fp-1"
    )
    second = await ctx.api.add_result(
        entity_type="observable", entity_id="o1", fingerprint="fp-1", verdict="info"
    )
    assert first["created"] is True
    assert second["created"] is False
    assert second["id"] == first["id"]
    assert len(ctx.results) == 1  # the repeat did not create a second result


async def test_add_result_or_permission_via_enrichment_only():
    # The OR half of _require_any: write:observable_enrichment alone must permit
    # add_result and upload_file, exactly as the runtime allows.
    ctx = FakeContext(manifest={"permissions": ["write:observable_enrichment"]})
    out = await ctx.api.add_result(
        entity_type="observable", entity_id="o1", fingerprint="fp-x"
    )
    assert out["created"] is True
    uploaded = await ctx.api.upload_file(b"bytes", "f.bin")
    assert uploaded["file_ref"].startswith("plugin-run-file:")
    ctx.assert_no_permission_violations()


# --- Reads ---


async def test_read_unseeded_entity_raises_lookup():
    ctx = FakeContext(manifest={"permissions": ["read:observable"]})
    with pytest.raises(LookupError, match="observable"):
        await ctx.api.get_observable("nope")


# --- File helpers ---


async def test_upload_and_download_file():
    ctx = FakeContext(
        manifest={"permissions": ["write:plugin_result"]},
        downloads={"observable:abc": b"screenshot-bytes"},
    )
    out = await ctx.api.upload_file(b"report-bytes", "report.pdf", "application/pdf")
    assert out["file_ref"].startswith("plugin-run-file:")
    assert out["size"] == len(b"report-bytes")
    assert ctx.uploaded_files[0]["filename"] == "report.pdf"

    assert await ctx.api.download_file("observable:abc") == b"screenshot-bytes"


async def test_upload_file_requires_write_permission():
    ctx = FakeContext(manifest={"permissions": ["read:observable"]})
    with pytest.raises(PermissionDenied):
        await ctx.api.upload_file(b"x", "x.bin")


# --- Error classification through ctx.http ---


async def test_http_auth_error_is_config_error():
    ctx = FakeContext(
        manifest=MANIFEST, http=fake_http(lambda r: httpx.Response(403))
    )
    with pytest.raises(ConfigError):
        await EnrichingPlugin().process(observable_event(data="1.2.3.4"), ctx)
    await ctx.http.aclose()


async def test_http_5xx_is_transient_error():
    ctx = FakeContext(
        manifest=MANIFEST, http=fake_http(lambda r: httpx.Response(503))
    )
    with pytest.raises(TransientError):
        await EnrichingPlugin().process(observable_event(data="1.2.3.4"), ctx)
    await ctx.http.aclose()


# --- Event factories ---


def test_event_factories_build_valid_envelopes():
    obs = observable_event(observable_id="o9", observable_type="domain", data="x.com")
    assert obs.object_type == "observable"
    assert obs.object_id == "o9"
    assert obs.data["observable_type"] == "domain"

    case = case_event(case_id=42)
    assert case.object_type == "case"
    assert case.object_id == "42"

    alert = alert_event(alert_id=7, event_type="alert.updated")
    assert alert.object_type == "alert"
    assert alert.event_type == "alert.updated"


# --- FakeContext is a drop-in PluginContext ---


def test_fakecontext_is_a_plugin_context():
    from catlico_plugin_sdk.models import PluginContext

    ctx = FakeContext(manifest=MANIFEST)
    assert isinstance(ctx, PluginContext)
    assert ctx.has_permission("read:observable") is True
    assert ctx.has_permission("write:case") is False
    assert isinstance(ctx.api, FakeCatlicoApi)


async def test_ctx_progress_records_via_api():
    ctx = FakeContext(manifest=MANIFEST)
    await ctx.progress("halfway", 50)
    assert ctx.progress_updates == [{"message": "halfway", "percent": 50}]

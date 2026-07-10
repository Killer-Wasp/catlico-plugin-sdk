"""SDK runtime client + HTTP helper: error classification and request shaping."""
import httpx
import pytest

from catlico_plugin_sdk import ConfigError, PluginApiClient, PluginHttp, TransientError


def _transport(handler):
    return httpx.MockTransport(handler)


async def test_http_classifies_auth_as_config_error():
    http = PluginHttp(transport=_transport(lambda r: httpx.Response(403)))
    with pytest.raises(ConfigError):
        await http.get("https://vendor.test/x")
    await http.aclose()


async def test_http_classifies_5xx_and_429_as_transient():
    for code in (429, 500, 503):
        http = PluginHttp(transport=_transport(lambda r, c=code: httpx.Response(c)))
        with pytest.raises(TransientError):
            await http.get("https://vendor.test/x")
        await http.aclose()


async def test_http_returns_ok_response():
    http = PluginHttp(
        transport=_transport(lambda r: httpx.Response(200, json={"score": 9}))
    )
    data = await http.get_json("https://vendor.test/x")
    assert data == {"score": 9}
    await http.aclose()


async def test_api_client_targets_runtime_prefix_with_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "obs-1"})

    client = PluginApiClient(
        "http://catlico:8000", "run-token-123", transport=_transport(handler)
    )
    result = await client.get_observable("obs-1")
    assert result == {"id": "obs-1"}
    assert seen["url"].endswith("/api/internal/plugin-runtime/observables/obs-1")
    assert seen["auth"] == "Bearer run-token-123"
    await client.aclose()


async def test_api_client_add_result_posts_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": "res-1", "created": True})

    client = PluginApiClient(
        "http://catlico:8000", "tok", transport=_transport(handler)
    )
    out = await client.add_result(
        entity_type="observable", entity_id="obs-1", fingerprint="fp-1"
    )
    assert out["created"] is True
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/plugin-runtime/results")
    await client.aclose()

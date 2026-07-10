"""Plugin runtime API client and outbound HTTP helper.

``ctx.api`` routes plugin reads/writes through Catlico's internal plugin-runtime
API using the run-scoped token. ``ctx.http`` is a thin wrapper over outbound
vendor calls that auto-classifies failures into the SDK's typed exceptions so
retry semantics come for free.

The client accepts an injectable transport so plugin tests never hit the network
(``PluginApiClient(..., transport=respx_or_fake)``).
"""
from __future__ import annotations

from typing import Any

import httpx

from catlico_plugin_sdk.plugin import ConfigError, TransientError


class PluginApiClient:
    """Talks to ``/api/internal/plugin-runtime/*`` with the run token."""

    def __init__(
        self,
        base_url: str,
        run_token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ):
        self._prefix = f"{base_url.rstrip('/')}/api/internal/plugin-runtime"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {run_token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PluginApiClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _json(self, method: str, path: str, **kw) -> Any:
        resp = await self._client.request(method, f"{self._prefix}{path}", **kw)
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.content

    # --- Reads ---

    async def get_observable(self, observable_id: str) -> dict:
        return await self._json("GET", f"/observables/{observable_id}")

    async def get_case(self, case_id: int) -> dict:
        return await self._json("GET", f"/cases/{case_id}")

    async def get_alert(self, alert_id: int) -> dict:
        return await self._json("GET", f"/alerts/{alert_id}")

    # --- Evidence (default output) ---

    async def add_result(self, **body) -> dict:
        return await self._json("POST", "/results", json=body)

    async def add_observable_enrichment(
        self, observable_id: str, *, source: str, data: dict, **body
    ) -> dict:
        payload = {"source": source, "data": data, **body}
        return await self._json(
            "POST", f"/observables/{observable_id}/enrichments", json=payload
        )

    # --- Proposed canonical mutations (require analyst approval) ---

    async def propose_case_patch(self, case_id: int, **fields) -> dict:
        return await self._json("PATCH", f"/cases/{case_id}", json=fields)

    async def propose_task(self, case_id: int, **fields) -> dict:
        return await self._json("POST", f"/cases/{case_id}/tasks", json=fields)

    async def propose_tag(self, case_id: int, tag: str) -> dict:
        return await self._json("POST", f"/cases/{case_id}/tags", json={"tag": tag})

    # --- Progress + files ---

    async def progress(self, message: str, percent: int | None = None) -> dict:
        return await self._json(
            "POST", "/progress", json={"message": message, "percent": percent}
        )

    async def upload_file(
        self, content: bytes, filename: str, content_type: str = "application/octet-stream"
    ) -> dict:
        return await self._json(
            "POST", "/files", files={"file": (filename, content, content_type)}
        )

    async def download_file(self, file_ref: str) -> bytes:
        return await self._json("GET", f"/files/{file_ref}")


class PluginHttp:
    """Outbound HTTP for plugin vendor calls, with failure auto-classification:
    401/403 -> ConfigError, 429/5xx/network -> TransientError."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            resp = await self._client.request(method, url, **kw)
        except httpx.TimeoutException as exc:
            raise TransientError(f"request to {url} timed out") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"request to {url} failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ConfigError(f"{url} returned {resp.status_code}: credential rejected")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"{url} returned {resp.status_code}")
        return resp

    async def get(self, url: str, **kw) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def get_json(self, url: str, **kw) -> Any:
        return (await self.get(url, **kw)).json()

    async def post(self, url: str, **kw) -> httpx.Response:
        return await self.request("POST", url, **kw)

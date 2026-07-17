"""Test kit for plugin authors — a fake runtime with no live Catlico API.

``FakeContext`` is a drop-in for the real ``PluginContext``: pass it anywhere a
plugin receives ``ctx``. It is backed by ``FakeCatlicoApi``, which enforces the
plugin's *manifest permissions by the same rules the Catlico runtime enforces*
(``app/api/internal/routes/plugin_runtime.py``): a call requiring a permission the
manifest did not declare fails, exactly as the runtime returns 403. Emitted
results, uploaded files, and progress updates are recorded so tests can assert on
them.

Recorded writes must also be **JSON-serializable**, because the runtime ships them
to the API as an httpx ``json=`` body: a plugin that emits a ``datetime`` or a
vendor SDK object (say a geoip2 ``IPv4Address``) fails its run in production, so
the fake raises ``TypeError`` for it here rather than storing the live object and
letting the test pass. ``upload_file`` is exempt — it posts bytes as multipart.

Typical use::

    from catlico_plugin_sdk.testing import FakeContext, observable_event, fake_http

    ctx = FakeContext(
        manifest=MANIFEST,                 # permissions come from here
        secrets={"key": "test-key"},
        http=fake_http(lambda r: httpx.Response(200, json={...})),
    )
    await MyPlugin().process(observable_event(data="1.2.3.4"), ctx)
    assert ctx.results[0]["verdict"] == "malicious"
    ctx.assert_no_permission_violations()
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Callable

import httpx

from catlico_plugin_sdk.api import PluginHttp
from catlico_plugin_sdk.app import Catlico
from catlico_plugin_sdk.models import PluginContext, PluginEvent


class PermissionDenied(RuntimeError):
    """A plugin made a runtime call its manifest permissions do not grant.

    The real Catlico runtime rejects such a call with 403; the fake raises this
    instead so tests can assert on the exact permission that was missing. Every
    denial is also recorded on ``FakeCatlicoApi.permission_denials``.
    """

    def __init__(self, method: str, required: frozenset[str]):
        self.method = method
        self.required = required
        want = " or ".join(sorted(required))
        super().__init__(
            f"{method} requires plugin permission {want}, "
            "which the manifest does not declare"
        )


#: Types the stdlib JSON encoder accepts as leaves (and as dict keys).
_JSON_SCALARS = (str, int, float, bool, type(None))


def _locate_unserializable(value: Any, path: str = "") -> tuple[str, Any] | None:
    """Path and value of the first item the JSON encoder would reject, if any.

    Only walked to explain a ``TypeError`` ``json.dumps`` already raised, so the
    happy path stays a single encode.
    """
    if isinstance(value, _JSON_SCALARS):
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, _JSON_SCALARS):
                return (path, key)
            found = _locate_unserializable(item, f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _locate_unserializable(item, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    return (path, value)


def _assert_json_serializable(what: str, payload: Any) -> None:
    """Reject a payload the real API boundary could not encode.

    Every runtime write is shipped to Catlico as an httpx ``json=`` body, so a
    value the stdlib encoder cannot handle — a ``datetime``, an ``IPv4Address``, a
    vendor SDK object — raises ``TypeError`` there and fails the run. The fake
    records into a plain list, which would happily hold such a value and let a
    green test ship a plugin that dies on its first real call; encoding here keeps
    the fake honest to that contract, and names the offending field.
    """
    try:
        json.dumps(payload)
    except TypeError as exc:
        found = _locate_unserializable(payload)
        if found is None:  # pragma: no cover - encoder and walker disagree
            raise
        path, value = found
        raise TypeError(f"{exc} — at {what}{path} = {value!r}") from None


class FakeCatlicoApi:
    """Stands in for ``ctx.api`` (the runtime ``PluginApiClient``).

    Enforces permissions and records everything the plugin emits. Reads are
    served from seed fixtures passed at construction; a read of an unseeded entity
    raises ``LookupError`` (the runtime returns 404).

    The method → permission mapping matches ``plugin_runtime.py`` exactly:

    ============================  ==========================================
    method                        required permission(s)
    ============================  ==========================================
    ``get_case``                  ``read:case``
    ``get_alert``                 ``read:alert``
    ``get_observable``            ``read:observable``
    ``add_result``                ``write:plugin_result`` **or**
                                  ``write:observable_enrichment``
    ``add_observable_enrichment`` ``write:observable_enrichment``
    ``upload_file``               ``write:plugin_result`` **or**
                                  ``write:observable_enrichment``
    ``download_file``             (none — scoped by the run token)
    ``progress``                  (none)
    ``propose_case_patch``        ``write:case``
    ``propose_task``              ``write:task``
    ``propose_tag``               ``write:case``
    ============================  ==========================================
    """

    def __init__(
        self,
        *,
        permissions: set[str] | None = None,
        cases: dict[Any, dict] | None = None,
        alerts: dict[Any, dict] | None = None,
        observables: dict[Any, dict] | None = None,
        downloads: dict[str, bytes] | None = None,
    ):
        self.permissions: set[str] = set(permissions or ())
        self._cases = {str(k): v for k, v in (cases or {}).items()}
        self._alerts = {str(k): v for k, v in (alerts or {}).items()}
        self._observables = {str(k): v for k, v in (observables or {}).items()}
        self._downloads = dict(downloads or {})

        # Recordings for test assertions.
        self.results: list[dict] = []
        self.uploaded_files: list[dict] = []
        self.progress_updates: list[dict] = []
        self.permission_denials: list[PermissionDenied] = []
        # fingerprint -> result id, for the run-scoped dedup the runtime enforces.
        self._result_ids: dict[str, str] = {}

    # --- Permission enforcement (mirrors runtime _require / _require_any) ---

    def _require(self, method: str, permission: str) -> None:
        if permission not in self.permissions:
            self._deny(method, frozenset({permission}))

    def _require_any(self, method: str, permissions: set[str]) -> None:
        if self.permissions.isdisjoint(permissions):
            self._deny(method, frozenset(permissions))

    def _deny(self, method: str, required: frozenset[str]) -> None:
        error = PermissionDenied(method, required)
        self.permission_denials.append(error)
        raise error

    # --- Reads ---

    async def get_case(self, case_id: int) -> dict:
        self._require("get_case", "read:case")
        return self._seeded(self._cases, case_id, "case")

    async def get_alert(self, alert_id: int) -> dict:
        self._require("get_alert", "read:alert")
        return self._seeded(self._alerts, alert_id, "alert")

    async def get_observable(self, observable_id: str) -> dict:
        self._require("get_observable", "read:observable")
        return self._seeded(self._observables, observable_id, "observable")

    @staticmethod
    def _seeded(store: dict[str, dict], entity_id: Any, kind: str) -> dict:
        try:
            return store[str(entity_id)]
        except KeyError:
            raise LookupError(
                f"no fake {kind} seeded for id {entity_id!r} — "
                f"pass {kind}s={{{entity_id!r}: {{...}}}} to FakeContext"
            ) from None

    # --- Evidence (default output) ---

    async def add_result(self, **body) -> dict:
        self._require_any(
            "add_result", {"write:plugin_result", "write:observable_enrichment"}
        )
        # The runtime requires entity_type/entity_id (subscripted directly) and a
        # truthy fingerprint (422 otherwise), then dedups on fingerprint per run.
        for field in ("entity_type", "entity_id", "fingerprint"):
            if not body.get(field):
                raise ValueError(f"add_result requires '{field}'")
        _assert_json_serializable("add_result", body)
        fingerprint = body["fingerprint"]
        existing = self._result_ids.get(fingerprint)
        if existing is not None:
            return {"id": existing, "created": False}
        record = {"kind": "result", **body}
        self.results.append(record)
        result_id = f"fake-result-{len(self.results)}"
        self._result_ids[fingerprint] = result_id
        return {"id": result_id, "created": True}

    async def add_observable_enrichment(
        self, observable_id: str, *, source: str, data: dict, **body
    ) -> dict:
        self._require("add_observable_enrichment", "write:observable_enrichment")
        _assert_json_serializable(
            "add_observable_enrichment", {"source": source, "data": data, **body}
        )
        record = {
            "kind": "enrichment",
            "observable_id": observable_id,
            "source": source,
            "data": data,
            **body,
        }
        self.results.append(record)
        return {"id": f"fake-result-{len(self.results)}", "ok": True}

    # --- Proposed canonical mutations (require analyst approval) ---

    async def propose_case_patch(self, case_id: int, **fields) -> dict:
        self._require("propose_case_patch", "write:case")
        return self._propose("patch_case_description", "case", case_id, fields)

    async def propose_task(self, case_id: int, **fields) -> dict:
        self._require("propose_task", "write:task")
        return self._propose("create_task", "case", case_id, fields)

    async def propose_tag(self, case_id: int, tag: str) -> dict:
        self._require("propose_tag", "write:case")
        return self._propose("add_tag", "case", case_id, {"tag": tag})

    def _propose(
        self, action_type: str, entity_type: str, entity_id: Any, payload: dict
    ) -> dict:
        _assert_json_serializable(action_type, payload)
        action = {
            "kind": "proposed_action",
            "action_type": action_type,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "payload": payload,
        }
        self.results.append(action)
        return {
            "proposed_action_id": f"fake-action-{len(self.results)}",
            "status": "proposed",
        }

    # --- Progress + files ---

    async def progress(self, message: str, percent: int | None = None) -> dict:
        record = {"message": message, "percent": percent}
        _assert_json_serializable("progress", record)
        self.progress_updates.append(record)
        return record

    async def upload_file(
        self,
        content: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> dict:
        self._require_any(
            "upload_file", {"write:plugin_result", "write:observable_enrichment"}
        )
        sha256 = hashlib.sha256(content).hexdigest()
        file_ref = f"plugin-run-file:{uuid.uuid4()}"
        record = {
            "file_ref": file_ref,
            "filename": filename,
            "content_type": content_type,
            "size": len(content),
            "sha256": sha256,
            "content": content,
        }
        self.uploaded_files.append(record)
        return {k: v for k, v in record.items() if k != "content"}

    async def download_file(self, file_ref: str) -> bytes:
        # No permission gate: the real runtime scopes downloads by the run token,
        # not a manifest permission. Seed bytes via ``downloads={ref: b"..."}``.
        try:
            return self._downloads[file_ref]
        except KeyError:
            raise LookupError(
                f"no fake download seeded for {file_ref!r} — "
                "pass downloads={ref: b'...'} to FakeContext"
            ) from None


class FakeContext(PluginContext):
    """A drop-in ``PluginContext`` backed by ``FakeCatlicoApi``.

    Permissions default to those declared in ``manifest['permissions']`` — the
    same source the runtime uses to mint a run token — so a plugin can only do
    what its manifest allows. Supply ``config``/``secrets`` fixtures directly.
    ``ctx.http`` defaults to ``None``; pass a mocked ``PluginHttp`` (see
    ``fake_http``) when the plugin makes vendor calls.
    """

    def __init__(
        self,
        *,
        manifest: dict | None = None,
        permissions: set[str] | None = None,
        config: dict | None = None,
        secrets: dict | None = None,
        http: PluginHttp | None = None,
        api: FakeCatlicoApi | None = None,
        run_id: str = "run-fake",
        plugin_id: str | None = None,
        plugin_version: str | None = None,
        organisation_id: str = "org-fake",
        event_id: str = "audit:fake",
        cases: dict[Any, dict] | None = None,
        alerts: dict[Any, dict] | None = None,
        observables: dict[Any, dict] | None = None,
        downloads: dict[str, bytes] | None = None,
    ):
        manifest = manifest or {}
        perms = (
            set(permissions)
            if permissions is not None
            else set(manifest.get("permissions", []))
        )
        fake_api = api or FakeCatlicoApi(
            permissions=perms,
            cases=cases,
            alerts=alerts,
            observables=observables,
            downloads=downloads,
        )
        super().__init__(
            run_id=run_id,
            plugin_id=plugin_id or manifest.get("id", "fake-plugin"),
            plugin_version=plugin_version or manifest.get("version", "0.0.0"),
            organisation_id=organisation_id,
            event_id=event_id,
            permissions=perms,
            api=fake_api,
            http=http,
            config=dict(config or {}),
            secrets=dict(secrets or {}),
        )

    # Passthroughs to the fake API's recordings, for readable assertions.

    @property
    def results(self) -> list[dict]:
        return self.api.results

    @property
    def uploaded_files(self) -> list[dict]:
        return self.api.uploaded_files

    @property
    def progress_updates(self) -> list[dict]:
        return self.api.progress_updates

    @property
    def permission_denials(self) -> list[PermissionDenied]:
        return self.api.permission_denials

    def assert_no_permission_violations(self) -> None:
        """Fail if the plugin attempted any call outside its manifest permissions."""
        if self.permission_denials:
            attempts = ", ".join(str(d) for d in self.permission_denials)
            raise AssertionError(f"plugin made forbidden runtime calls: {attempts}")


# --- Event factories ---


def make_envelope(
    *,
    object_type: str,
    object_id: str,
    event_type: str,
    data: dict | None = None,
    organisation_id: str = "org-fake",
    event_id: str = "audit:fake",
    context: dict | None = None,
    occurred_at: str = "",
    actor: str = "",
    attempt: int = 1,
) -> dict:
    """Build a raw event envelope dict (the shape the runner delivers)."""
    return {
        "event_id": event_id,
        "event_type": event_type,
        "organisation_id": organisation_id,
        "object": {"type": object_type, "id": object_id},
        "data": data or {},
        "context": context or {},
        "occurred_at": occurred_at,
        "actor": actor,
        "attempt": attempt,
    }


def observable_event(
    *,
    observable_id: str = "obs-1",
    observable_type: str = "ip",
    data: str = "1.2.3.4",
    event_type: str = "observable.created",
    tlp: int = 2,
    pap: int = 2,
    extra: dict | None = None,
    **envelope_kw,
) -> PluginEvent:
    """An ``observable.*`` event for an observable of ``observable_type``."""
    payload = {"observable_type": observable_type, "data": data, "tlp": tlp, "pap": pap}
    if extra:
        payload.update(extra)
    return PluginEvent.from_envelope(
        make_envelope(
            object_type="observable",
            object_id=observable_id,
            event_type=event_type,
            data=payload,
            **envelope_kw,
        )
    )


def case_event(
    *,
    case_id: int | str = 1,
    event_type: str = "case.created",
    data: dict | None = None,
    **envelope_kw,
) -> PluginEvent:
    """A ``case.*`` event for the given case id."""
    return PluginEvent.from_envelope(
        make_envelope(
            object_type="case",
            object_id=str(case_id),
            event_type=event_type,
            data=data,
            **envelope_kw,
        )
    )


def alert_event(
    *,
    alert_id: int | str = 1,
    event_type: str = "alert.created",
    data: dict | None = None,
    **envelope_kw,
) -> PluginEvent:
    """An ``alert.*`` event for the given alert id."""
    return PluginEvent.from_envelope(
        make_envelope(
            object_type="alert",
            object_id=str(alert_id),
            event_type=event_type,
            data=data,
            **envelope_kw,
        )
    )


async def run_app(app: Catlico, event: PluginEvent, ctx: PluginContext) -> dict:
    """Dispatch ``event`` through ``app`` and return the result dict.

    The test-kit counterpart to the worker's dispatch: drives a whole
    ``Catlico`` app (matchers + handlers + error classification) end-to-end
    against a ``FakeContext``, so an integration test can assert on the run's
    ``status``/``skip_reason``/``error_kind`` exactly as the runner would see them.
    Call a single handler directly when you only need to unit-test one function.
    """
    return await app.dispatch(event, ctx)


def fake_http(handler: Callable[[httpx.Request], httpx.Response]) -> PluginHttp:
    """A ``PluginHttp`` whose requests are served by ``handler`` (no network).

    Handy for exercising a plugin's vendor calls and the SDK's error
    classification (return a 403 to get a ``ConfigError``, a 500/429 to get a
    ``TransientError``)."""
    return PluginHttp(transport=httpx.MockTransport(handler))


__all__ = [
    "FakeCatlicoApi",
    "FakeContext",
    "PermissionDenied",
    "alert_event",
    "case_event",
    "fake_http",
    "make_envelope",
    "observable_event",
    "run_app",
]

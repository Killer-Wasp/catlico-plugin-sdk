"""The ``Catlico`` app — the decorator authoring surface for Catlico plugins.

A plugin is a module that constructs one ``Catlico`` instance and decorates plain
functions with ``@catlico.event(...)`` (and, optionally, ``@catlico.health()``).
The manifest's ``entrypoint`` points at that instance, e.g.
``entrypoint = "main:catlico"``.

This is a Bolt-style API: the decorators return the wrapped function *unchanged*,
so a handler stays a normal ``async def`` that a test can call directly. The app
only records the registrations; ``dispatch`` and ``check_health`` drive them.

    from catlico_plugin_sdk import Catlico, SkipRun

    catlico = Catlico()

    @catlico.event("observable.created", matchers=[lambda e: e.data.get("observable_type") == "ip"])
    async def enrich(event, ctx):
        ...

    @catlico.health()
    async def health(ctx):
        return {"ok": True}

Filtering has two shapes, both of which classify the run as ``skipped`` (so the
API's skipped-run accounting is preserved):

* ``matchers=[...]`` — cheap envelope filters evaluated *before* the handler
  runs. Each is a sync-or-async callable taking ``(event)`` or ``(event, ctx)``;
  return falsey to reject. A handler none of whose matchers accept is not run.
* ``raise SkipRun(reason)`` — an in-handler filter for decisions that need work
  (a lookup, a config read) before you know the event is irrelevant.
"""
from __future__ import annotations

import inspect
import traceback
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from catlico_plugin_sdk.plugin import PluginRuntimeError

#: A handler/matcher takes ``(event, ctx)`` (or just ``(event)`` for a matcher).
Handler = Callable[..., Awaitable[None]]
Matcher = Callable[..., Any]


class SkipRun(Exception):
    """Raise inside a handler to end the run as ``skipped`` (not a failure).

    The in-handler counterpart to a ``matchers=`` envelope filter: use it when
    deciding the event is irrelevant needs work the matcher can't cheaply do.
    """

    def __init__(self, reason: str = "skipped by plugin"):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class _Registration:
    handler: Handler
    matchers: tuple[Matcher, ...]


def _error_result(exc: BaseException) -> dict:
    """Classify a handler exception into a failure result.

    Centralises the exception→``error_kind`` mapping that used to be duplicated
    across the worker and the CLI: a ``PluginRuntimeError`` carries its own
    ``error_kind`` (config/transient/input/bug); anything else is a ``bug``.
    """
    kind = getattr(exc, "error_kind", "bug") if isinstance(exc, PluginRuntimeError) else "bug"
    return {
        "status": "failure",
        "error": f"{type(exc).__name__}: {exc}",
        "error_kind": kind,
    }


class Catlico:
    """A plugin app: a registry of event handlers plus an optional health check."""

    def __init__(self) -> None:
        # Insertion-ordered: dispatch runs an event's handlers in registration order.
        self._handlers: dict[str, list[_Registration]] = {}
        self._health: Handler | None = None

    # --- Registration (decorators) ---------------------------------------

    def event(
        self,
        event_type: str | list[str],
        *,
        matchers: tuple[Matcher, ...] | list[Matcher] = (),
    ) -> Callable[[Handler], Handler]:
        """Register ``fn`` to handle one or more event types. Returns ``fn`` unchanged.

        ``event_type`` is a single event name or a list of them. Pass a list to
        register a single handler for several events, e.g.
        ``@catlico.event(["observable.created", "observable.manual"])`` — the one
        handler runs (with the same ``matchers``) for each. Multiple handlers may
        also register for the same event; ``dispatch`` runs them in registration
        order. ``matchers`` are cheap envelope filters (see the module docstring).
        """
        event_types = [event_type] if isinstance(event_type, str) else list(event_type)
        if not event_types:
            raise ValueError("@catlico.event() requires at least one event type")
        registration_matchers = tuple(matchers)

        def decorator(fn: Handler) -> Handler:
            for et in event_types:
                self._handlers.setdefault(et, []).append(
                    _Registration(fn, registration_matchers)
                )
            return fn

        return decorator

    def health(self) -> Callable[[Handler], Handler]:
        """Register the plugin's health check. At most one may be registered."""

        def decorator(fn: Handler) -> Handler:
            if self._health is not None:
                raise RuntimeError(
                    "a second @catlico.health() handler was registered; "
                    "only one health check is allowed per plugin"
                )
            self._health = fn
            return fn

        return decorator

    # --- Introspection ---------------------------------------------------

    @property
    def registered_events(self) -> frozenset[str]:
        """The set of event types this app has handlers for.

        The worker validates this equals the manifest's declared ``triggers`` so
        routing metadata and code can never silently drift apart.
        """
        return frozenset(self._handlers)

    # --- Execution -------------------------------------------------------

    async def dispatch(self, event: Any, ctx: Any) -> dict:
        """Run every matching handler for ``event`` and return a result dict.

        Semantics:
        * No handler registered for the event type → ``skipped``.
        * Handlers run in registration order. A handler whose matchers all pass
          runs; one whose matchers reject is skipped.
        * The first handler to raise a non-``SkipRun`` exception fails the run
          (``failure`` with the mapped ``error_kind``); later handlers don't run.
        * A handler that raises ``SkipRun`` is treated as skipped.
        * If at least one handler completed, the run is ``success``; if every
          handler was skipped (matchers rejected or ``SkipRun``), it's ``skipped``.
        """
        registrations = self._handlers.get(event.event_type, [])
        if not registrations:
            return {
                "status": "skipped",
                "skip_reason": f"no handler registered for event type {event.event_type!r}",
            }

        completed = 0
        skip_reason: str | None = None
        for registration in registrations:
            if not await self._matches(registration.matchers, event, ctx):
                skip_reason = skip_reason or "no matcher accepted the event"
                continue
            try:
                await registration.handler(event, ctx)
            except SkipRun as exc:
                skip_reason = exc.reason
            except PluginRuntimeError as exc:
                return _error_result(exc)
            except Exception as exc:  # noqa: BLE001 — any handler error becomes a result
                traceback.print_exc()
                result = _error_result(exc)
                result["traceback"] = traceback.format_exc()
                return result
            else:
                completed += 1

        if completed == 0:
            return {"status": "skipped", "skip_reason": skip_reason or "no handler matched"}
        return {"status": "success"}

    async def check_health(self, ctx: Any) -> dict:
        """Run the registered health check (or return ``{"ok": True}`` if none).

        Exceptions propagate: the worker/runner classifies a failing health check
        the same way it classifies a failing run.
        """
        if self._health is None:
            return {"ok": True}
        result = self._health(ctx)
        if inspect.isawaitable(result):
            result = await result
        return result if result is not None else {"ok": True}

    # --- Internals -------------------------------------------------------

    @staticmethod
    async def _matches(matchers: tuple[Matcher, ...], event: Any, ctx: Any) -> bool:
        for matcher in matchers:
            if not await Catlico._eval_matcher(matcher, event, ctx):
                return False
        return True

    @staticmethod
    async def _eval_matcher(matcher: Matcher, event: Any, ctx: Any) -> bool:
        """Call a matcher, passing ``(event)`` or ``(event, ctx)`` per its arity."""
        try:
            params = inspect.signature(matcher).parameters
            wants_ctx = len(params) >= 2 or any(
                p.kind is inspect.Parameter.VAR_POSITIONAL for p in params.values()
            )
        except (TypeError, ValueError):
            wants_ctx = False
        result = matcher(event, ctx) if wants_ctx else matcher(event)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)


__all__ = ["Catlico", "SkipRun"]

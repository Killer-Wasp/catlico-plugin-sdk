"""The ``Catlico`` app: registration, dispatch matrix, matchers, SkipRun, errors."""
import pytest

from catlico_plugin_sdk import (
    Catlico,
    ConfigError,
    InputError,
    SkipRun,
    TransientError,
)
from catlico_plugin_sdk.testing import FakeContext, observable_event, run_app


def _ctx() -> FakeContext:
    return FakeContext(permissions={"read:observable", "write:observable_enrichment"})


# --- registration ------------------------------------------------------------


def test_event_decorator_returns_function_unchanged():
    catlico = Catlico()

    async def handler(event, ctx):
        return "sentinel"

    decorated = catlico.event("observable.created")(handler)
    assert decorated is handler


def test_registered_events_reflects_decorators():
    catlico = Catlico()

    @catlico.event("observable.created")
    async def a(event, ctx): ...

    @catlico.event("case.created")
    async def b(event, ctx): ...

    assert catlico.registered_events == frozenset(
        {"observable.created", "case.created"}
    )


def test_one_handler_registers_for_a_list_of_event_types():
    catlico = Catlico()

    @catlico.event(["observable.created", "observable.manual"])
    async def handler(event, ctx): ...

    assert catlico.registered_events == frozenset(
        {"observable.created", "observable.manual"}
    )
    # Same handler registered under each type.
    assert (
        catlico._handlers["observable.created"][0].handler
        is catlico._handlers["observable.manual"][0].handler
    )


def test_event_decorator_requires_an_event_type():
    catlico = Catlico()

    with pytest.raises(ValueError, match="at least one event type"):
        catlico.event([])


def test_second_health_registration_raises():
    catlico = Catlico()

    @catlico.health()
    async def h1(ctx): ...

    with pytest.raises(RuntimeError, match="one health"):
        @catlico.health()
        async def h2(ctx): ...


# --- dispatch: success / skip / failure --------------------------------------


async def test_dispatch_runs_handler_and_reports_success():
    catlico = Catlico()
    ran = []

    @catlico.event("observable.created")
    async def handler(event, ctx):
        ran.append(event.object_id)

    result = await run_app(catlico, observable_event(), _ctx())
    assert result["status"] == "success"
    assert ran == ["obs-1"]


async def test_dispatch_no_handler_for_event_type_is_skipped():
    catlico = Catlico()

    @catlico.event("case.created")
    async def handler(event, ctx): ...

    result = await catlico.dispatch(observable_event(), _ctx())
    assert result["status"] == "skipped"
    assert "no handler registered" in result["skip_reason"]


async def test_multiple_handlers_run_in_registration_order():
    catlico = Catlico()
    order = []

    @catlico.event("observable.created")
    async def first(event, ctx):
        order.append("first")

    @catlico.event("observable.created")
    async def second(event, ctx):
        order.append("second")

    result = await catlico.dispatch(observable_event(), _ctx())
    assert result["status"] == "success"
    assert order == ["first", "second"]


# --- matchers ----------------------------------------------------------------


async def test_matcher_rejecting_skips_the_run():
    catlico = Catlico()

    @catlico.event(
        "observable.created",
        matchers=[lambda e: e.data.get("observable_type") == "ip"],
    )
    async def handler(event, ctx):
        raise AssertionError("must not run")

    event = observable_event(observable_type="domain", data="x.com")
    result = await catlico.dispatch(event, _ctx())
    assert result["status"] == "skipped"


async def test_matcher_accepting_runs_the_handler():
    catlico = Catlico()
    ran = []

    @catlico.event(
        "observable.created",
        matchers=[lambda e: e.data.get("observable_type") == "ip"],
    )
    async def handler(event, ctx):
        ran.append(True)

    result = await catlico.dispatch(observable_event(data="1.2.3.4"), _ctx())
    assert result["status"] == "success"
    assert ran == [True]


async def test_two_arg_and_async_matchers_supported():
    catlico = Catlico()

    async def wants_ip(event, ctx):
        assert ctx is not None  # 2-arg form receives ctx
        return event.data.get("observable_type") == "ip"

    @catlico.event("observable.created", matchers=[wants_ip])
    async def handler(event, ctx): ...

    ok = await catlico.dispatch(observable_event(data="1.2.3.4"), _ctx())
    assert ok["status"] == "success"

    skip = await catlico.dispatch(
        observable_event(observable_type="domain", data="x.com"), _ctx()
    )
    assert skip["status"] == "skipped"


# --- SkipRun -----------------------------------------------------------------


async def test_skiprun_marks_run_skipped_with_reason():
    catlico = Catlico()

    @catlico.event("observable.created")
    async def handler(event, ctx):
        raise SkipRun("not interesting")

    result = await catlico.dispatch(observable_event(), _ctx())
    assert result["status"] == "skipped"
    assert result["skip_reason"] == "not interesting"


async def test_one_handler_succeeding_wins_over_another_skipping():
    catlico = Catlico()

    @catlico.event("observable.created")
    async def skips(event, ctx):
        raise SkipRun("nope")

    @catlico.event("observable.created")
    async def works(event, ctx):
        return None

    result = await catlico.dispatch(observable_event(), _ctx())
    assert result["status"] == "success"


# --- error classification ----------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected_kind",
    [
        (ConfigError("bad key"), "config"),
        (TransientError("429"), "transient"),
        (InputError("nope"), "input"),
        (ValueError("boom"), "bug"),
    ],
)
async def test_error_kind_mapping(exc, expected_kind):
    catlico = Catlico()

    @catlico.event("observable.created")
    async def handler(event, ctx):
        raise exc

    result = await catlico.dispatch(observable_event(), _ctx())
    assert result["status"] == "failure"
    assert result["error_kind"] == expected_kind
    assert type(exc).__name__ in result["error"]


async def test_first_raise_stops_later_handlers():
    catlico = Catlico()
    ran = []

    @catlico.event("observable.created")
    async def boom(event, ctx):
        raise ValueError("boom")

    @catlico.event("observable.created")
    async def never(event, ctx):
        ran.append("never")

    result = await catlico.dispatch(observable_event(), _ctx())
    assert result["status"] == "failure"
    assert ran == []


# --- health ------------------------------------------------------------------


async def test_check_health_default_is_ok():
    catlico = Catlico()
    assert await catlico.check_health(_ctx()) == {"ok": True}


async def test_check_health_runs_registered_handler():
    catlico = Catlico()

    @catlico.health()
    async def health(ctx):
        return {"ok": True, "detail": "reachable"}

    assert await catlico.check_health(_ctx()) == {"ok": True, "detail": "reachable"}

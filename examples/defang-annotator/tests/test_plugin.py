"""Tests for the Defang Annotator example plugin.

Runnable from this directory (``uv run pytest``) or, since it's collected as
part of the SDK repo, from the SDK repo root (``uv run pytest``). Uses only
``catlico_plugin_sdk.testing`` — no real network, no live Catlico API.
"""
import pytest

from catlico_plugin_sdk.plugin import ConfigError, InputError
from catlico_plugin_sdk.testing import FakeContext, observable_event, run_app

from defang_annotator_plugin.plugin import defang, health, private_range_note, process

PERMISSIONS = {"read:observable", "write:observable_enrichment"}


def _ctx(**kw) -> FakeContext:
    return FakeContext(permissions=PERMISSIONS, **kw)


async def test_defangs_an_ip_with_default_bracket_style():
    ctx = _ctx()
    await process(observable_event(data="8.8.8.8"), ctx)

    assert ctx.results[0]["source"] == "Defang Annotator"
    assert ctx.results[0]["data"]["defanged"] == "8[.]8[.]8[.]8"
    assert ctx.results[0]["verdict"] == "info"
    ctx.assert_no_permission_violations()


async def test_flags_a_private_ip_range():
    ctx = _ctx()
    await process(observable_event(data="192.168.1.1"), ctx)

    notes = ctx.results[0]["data"]["notes"]
    assert notes == ["private (RFC1918/RFC4193) address"]
    assert "private" in ctx.results[0]["summary"]


async def test_annotate_private_ranges_config_can_be_disabled():
    ctx = _ctx(config={"annotate_private_ranges": False})
    await process(observable_event(data="10.0.0.1"), ctx)

    assert ctx.results[0]["data"]["notes"] == []


async def test_hxxp_style_defangs_the_scheme_and_dots():
    ctx = _ctx(config={"style": "hxxp"})
    event = observable_event(
        observable_type="url", data="https://evil.example/payload"
    )
    await process(event, ctx)

    assert ctx.results[0]["data"]["defanged"] == "hxxps://evil[.]example/payload"


async def test_domain_is_defanged_and_not_annotated_as_private():
    ctx = _ctx()
    event = observable_event(observable_type="domain", data="example.com")
    await process(event, ctx)

    assert ctx.results[0]["data"]["defanged"] == "example[.]com"
    assert ctx.results[0]["data"]["notes"] == []


async def test_empty_value_raises_input_error():
    ctx = _ctx()
    with pytest.raises(InputError):
        await process(observable_event(data=""), ctx)


async def test_unknown_style_raises_config_error():
    ctx = _ctx(config={"style": "leetspeak"})
    with pytest.raises(ConfigError):
        await process(observable_event(data="1.2.3.4"), ctx)


async def test_health_rejects_a_bad_style_override():
    ctx = _ctx(config={"style": "leetspeak"})
    with pytest.raises(ConfigError):
        await health(ctx)


# --- End-to-end through the app (matchers + dispatch), via run_app ----------


async def test_app_skips_unsupported_observable_types():
    """The matcher on the event handler filters out types we can't defang, so the
    whole run is 'skipped' with no enrichment written."""
    from main import catlico

    ctx = _ctx()
    event = observable_event(observable_type="hash", data="deadbeef")
    result = await run_app(catlico, event, ctx)

    assert result["status"] == "skipped"
    assert ctx.results == []


async def test_app_dispatches_supported_type_to_success():
    from main import catlico

    ctx = _ctx()
    result = await run_app(catlico, observable_event(data="8.8.8.8"), ctx)

    assert result["status"] == "success"
    assert ctx.results[0]["data"]["defanged"] == "8[.]8[.]8[.]8"


# --- Unit coverage of the pure helpers (no ctx needed) -----------------------


@pytest.mark.parametrize(
    "value,style,expected",
    [
        ("1.2.3.4", "brackets", "1[.]2[.]3[.]4"),
        ("http://evil.example", "brackets", "http[://]evil[.]example"),
        ("https://evil.example", "hxxp", "hxxps://evil[.]example"),
    ],
)
def test_defang_helper(value, style, expected):
    assert defang(value, style) == expected


@pytest.mark.parametrize(
    "ip,expected_substring",
    [
        ("127.0.0.1", "loopback"),
        ("169.254.1.1", "link-local"),
        ("192.168.0.1", "private"),
        ("8.8.8.8", None),
        ("not-an-ip", None),
    ],
)
def test_private_range_note_helper(ip, expected_substring):
    note = private_range_note(ip)
    if expected_substring is None:
        assert note is None
    else:
        assert expected_substring in note

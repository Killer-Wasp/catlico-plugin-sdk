"""Defang Annotator — example plugin for the Catlico Plugin SDK.

Takes an ``ip`` / ``domain`` / ``url`` observable and writes back a *defanged*
form (safe to paste into a chat or ticket without it becoming a clickable
link, e.g. ``1[.]2[.]3[.]4`` or ``hxxps[://]evil[.]example``), plus — for IP
observables — a note when the address falls in a loopback, link-local,
private (RFC1918), reserved, or multicast range.

This is intentionally **fully offline**: no ``ctx.http`` vendor call, no
secrets. It exists to show the *other* half of the SDK contract — using
``ctx.api``, a config value, and ``ctx.progress`` — without needing a network
connection or an API key to run. For a plugin that also demonstrates
``ctx.http`` and secrets, see ``catlico-plugins/abuseipdb``.

See ``README.md`` in this directory for how to validate, run, and test it,
and ``docs/quickstart.md`` in the SDK repo for a guided walkthrough that
builds a plugin like this one from scratch.
"""
from __future__ import annotations

import ipaddress

from catlico_plugin_sdk import CatlicoPlugin
from catlico_plugin_sdk.plugin import ConfigError, InputError

#: Observable types this plugin knows how to defang.
_SUPPORTED_TYPES = {"ip", "domain", "url", "hostname"}

#: Config values ``style`` accepts (also declared as `choices` in the manifest —
#: the manifest schema doesn't enforce choices at validate time, so process()
#: re-checks defensively in case an org-level override sets something else).
_STYLES = {"brackets", "hxxp"}


def defang(value: str, style: str) -> str:
    """Return ``value`` with its scheme/dots bracketed so it doesn't render
    as a live link or resolve by accident when pasted into chat/tickets."""
    out = value
    if style == "hxxp":
        out = out.replace("https://", "hxxps://").replace("http://", "hxxp://")
    else:
        out = out.replace("://", "[://]")
    return out.replace(".", "[.]")


def private_range_note(ip_value: str) -> str | None:
    """A short note if ``ip_value`` falls in a loopback/link-local/private/
    reserved/multicast range, else ``None`` (including for non-IP input —
    domains/URLs are simply not annotated this way)."""
    try:
        addr = ipaddress.ip_address(ip_value)
    except ValueError:
        return None
    if addr.is_loopback:
        return "loopback address"
    if addr.is_link_local:
        return "link-local address"
    if addr.is_private:
        return "private (RFC1918/RFC4193) address"
    if addr.is_reserved:
        return "reserved address"
    if addr.is_multicast:
        return "multicast address"
    return None


class DefangAnnotatorPlugin(CatlicoPlugin):
    triggers = ["observable.created"]

    async def health(self, ctx) -> dict:
        # No secrets to check, but a bad org-level config override should still
        # fail activation loudly rather than silently misbehaving per-event.
        style = ctx.config.get("style", "brackets")
        if style not in _STYLES:
            raise ConfigError(
                f"unknown defang style {style!r}; expected one of {sorted(_STYLES)}"
            )
        return {"ok": True}

    async def should_process(self, event, ctx) -> bool:
        # Cheap envelope filter — only observable types we know how to defang.
        return (
            event.event_type in self.triggers
            and event.data.get("observable_type") in _SUPPORTED_TYPES
        )

    async def process(self, event, ctx) -> None:
        value = (event.data.get("data") or "").strip()
        if not value:
            raise InputError("observable has no value to defang")

        style = ctx.config.get("style", "brackets")
        if style not in _STYLES:
            raise ConfigError(
                f"unknown defang style {style!r}; expected one of {sorted(_STYLES)}"
            )

        await ctx.progress(f"defanging {value}", percent=25)
        defanged = defang(value, style)

        notes: list[str] = []
        observable_type = event.data.get("observable_type", "")
        if observable_type == "ip" and ctx.config.get("annotate_private_ranges", True):
            note = private_range_note(value)
            if note:
                notes.append(note)

        await ctx.progress("writing enrichment", percent=75)

        summary = defanged if not notes else f"{defanged} — {'; '.join(notes)}"
        await ctx.api.add_observable_enrichment(
            event.object_id,
            source="Defang Annotator",
            data={"defanged": defanged, "style": style, "notes": notes},
            verdict="info",
            summary=summary,
        )

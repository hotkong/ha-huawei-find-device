"""Shared helpers: runtime container, identifiers, cookies and session notices."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .api import HuaweiFindClient
from .const import (
    CONF_COOKIE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GEOCODE_NONE,
    MAX_CONCURRENT_LOCATES,
)

_LOGGER = logging.getLogger(__name__)


@callback
def abort_reauth_flows(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Abort pending reauth flows for one entry (e.g. after an automatic login)."""
    flow_manager = getattr(hass.config_entries, "flow", None)
    if flow_manager is None:
        return
    for flow in flow_manager.async_progress():
        context = flow.get("context") or {}
        if context.get("entry_id") != entry.entry_id:
            continue
        if context.get("source") != SOURCE_REAUTH:
            continue
        flow_manager.async_abort(flow["flow_id"])


def device_identifiers(entry_id: str, device_id: str) -> set[tuple[str, str]]:
    """Device registry identifier, scoped per config entry (multi-account safe)."""
    return {(DOMAIN, f"{entry_id}:{device_id}")}


def supports_ring(device: dict[str, Any]) -> bool:
    """Own phones/wearables, plus devices that advertise the bell capability."""
    if device.get("is_shared"):
        return False
    if str(device.get("per_device_type") or "") in {"0", "2"}:
        return True
    return "bell" in (device.get("capability") or [])


def signal_update(entry_id: str, device_id: str) -> str:
    """Dispatcher signal for one device of one entry."""
    return f"{DOMAIN}_update_{entry_id}_{device_id}"


def supports_charging(device: dict[str, Any]) -> bool:
    """Only phones report a charging flag; wearables and earbuds do not."""
    per_type = str(device.get("per_device_type") or "")
    return per_type not in {"1", "2"}


def battery_components(device: dict[str, Any]) -> tuple[str, ...]:
    """Battery entities to create, driven by what the API says the device has."""
    known = tuple(str(name) for name in (device.get("components") or []))
    if known:
        ordered = tuple(
            name for name in ("case", "left", "right") if name in known
        )
        if ordered:
            return ordered
        if len(known) > 1:
            return tuple(dict.fromkeys(known))
        # A single "main" component is the device-level battery sensor.
        return ()
    # No cached locate result yet: fall back to the TWS hint from the API.
    if "bell" in (device.get("capability") or []) or str(
        device.get("per_device_type") or ""
    ) == "1":
        return ("left", "right")
    return ()


def signal_account(entry_id: str) -> str:
    """Dispatcher signal for account-level state (hub button)."""
    return f"{DOMAIN}_account_{entry_id}"


def scoped_device_id(entry_id: str, identifier: str) -> str | None:
    """Return the device id from an entry-scoped registry identifier."""
    prefix = f"{entry_id}:"
    return identifier[len(prefix) :] if identifier.startswith(prefix) else None


@callback
def prune_removed_devices(
    hass: HomeAssistant, entry: ConfigEntry, kept_ids: set[str]
) -> None:
    """Drop registry entries (entities and device) for deselected devices."""
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
        device_ids = [
            scoped_device_id(entry.entry_id, value)
            for domain, value in device.identifiers
            if domain == DOMAIN
        ]
        device_ids = [device_id for device_id in device_ids if device_id]
        if not device_ids:
            continue  # account hub device
        if any(device_id in kept_ids for device_id in device_ids):
            continue
        for entity_entry in er.async_entries_for_device(
            entity_reg, device.id, include_disabled_entities=True
        ):
            entity_reg.async_remove(entity_entry.entity_id)
        device_reg.async_remove_device(device.id)


def serialize_cookies(cookies: dict[str, str]) -> str:
    """Render a cookie jar as a Cookie request header value."""
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


@dataclass
class HuaweiFindRuntime:
    """Runtime data of one config entry (one Huawei account)."""

    entry: ConfigEntry
    client: HuaweiFindClient
    devices: dict[str, dict[str, Any]]
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    refreshing: set[str] = field(default_factory=set)
    unsub_heartbeat: Callable[[], None] | None = None
    store: Store | None = None
    geocode_provider: str = GEOCODE_NONE
    amap_key: str = ""
    save_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    auto_refresh_unsub: Callable[[], None] | None = None
    reauth_requested: bool = False
    browser_url: str = ""
    browser_token: str = ""
    browser_username: str = ""
    browser_password: str = ""
    login_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_login_attempt: float = 0.0
    last_login_success: Any = None
    login_error: str | None = None
    heartbeat_ok: bool | None = None
    last_heartbeat: Any = None
    heartbeat_failures: int = 0
    expiry_warned: bool = False
    unloading: bool = False
    tasks: set[asyncio.Task] = field(default_factory=set)
    last_ring: dict[str, float] = field(default_factory=dict)
    ring_errors: dict[str, str] = field(default_factory=dict)
    ring_results: dict[str, Any] = field(default_factory=dict)
    semaphore: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT_LOCATES)
    )


def _make_cookie_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> Callable[[dict[str, str]], None]:
    """Huawei Cloud rotates cookies (JSESSIONID etc.); save new ones back."""
    state = {"value": entry.data.get(CONF_COOKIE, "")}

    def listener(cookies: dict[str, str]) -> None:
        value = serialize_cookies(cookies)
        if value == state["value"]:
            return
        state["value"] = value
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_COOKIE: value}
        )

    return listener


def _interval_minutes(value: Any) -> int:
    """Parse the scan_interval option defensively.

    A missing option means "not configured yet", which now defaults to
    DEFAULT_SCAN_INTERVAL; an explicit 0 disables auto refresh, and an
    unparsable value stays disabled instead of guessing.
    """
    if value is None or value == "":
        return DEFAULT_SCAN_INTERVAL
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        _LOGGER.warning("Invalid scan_interval %r, auto refresh disabled", value)
        return 0


def _notify_reauth(hass: HomeAssistant, runtime: HuaweiFindRuntime, reason: str) -> None:
    """Ask the user to re-authenticate; only once until it succeeds again."""
    if runtime.reauth_requested:
        return
    runtime.reauth_requested = True
    has_fallback = bool(
        runtime.browser_url
        and runtime.browser_username
        and runtime.browser_password
    )
    if runtime.login_error:
        detail = f"\n\nAutomatic re-login failed: {runtime.login_error}"
    elif not has_fallback:
        detail = (
            "\n\nNo automatic re-login is configured; "
            "paste a fresh Cookie to restore the session."
        )
    else:
        detail = ""
    persistent_notification.async_create(
        hass,
        f"The Huawei Cloud session has expired ({reason}).{detail}\n\n"
        "Go to Settings > Devices & Services > Huawei Cloud Find Device "
        "and click Re-authenticate, then log in to cloud.huawei.com again "
        "and paste the new Cookie.",
        title="Huawei Cloud Find Device: re-authentication required",
        notification_id=f"{DOMAIN}_reauth_{runtime.entry.entry_id}",
    )
    runtime.entry.async_start_reauth(hass)

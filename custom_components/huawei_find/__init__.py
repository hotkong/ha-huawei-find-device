"""Huawei Cloud Find Device integration."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import HuaweiFindAuthError, HuaweiFindClient, HuaweiFindError
from .geocode import async_reverse_geocode
from .const import (
    CONF_AMAP_KEY,
    CONF_COOKIE,
    CONF_DEVICES,
    CONF_GEOCODE_PROVIDER,
    ADDRESS_KEEP_RADIUS,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    LOCATE_RETRIES,
    LOCATE_RETRY_DELAY,
    MAX_CONCURRENT_LOCATES,
    GEOCODE_NONE,
    SERVICE_REFRESH,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
]

SERVICE_SCHEMA = vol.Schema({})

# The portal pings heartbeatCheck while a browser tab is open; doing the same
# keeps the server-side session alive and rotates CSRFToken.
HEARTBEAT_INTERVAL = timedelta(minutes=5)


def signal_update(entry_id: str, device_id: str) -> str:
    return f"{DOMAIN}_update_{entry_id}_{device_id}"


def serialize_cookies(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


@dataclass
class HuaweiFindRuntime:
    """Runtime data."""

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
    auto_refresh_unsub: Callable[[], None] | None = None
    reauth_requested: bool = False
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


def _notify_reauth(hass: HomeAssistant, runtime: HuaweiFindRuntime, reason: str) -> None:
    """Ask the user to re-authenticate; only once until it succeeds again."""
    if runtime.reauth_requested:
        return
    runtime.reauth_requested = True
    persistent_notification.async_create(
        hass,
        f"The Huawei Cloud session has expired ({reason}).\n\n"
        "Go to Settings > Devices & Services > Huawei Cloud Find Device "
        "and click Re-authenticate, then log in to cloud.huawei.com again "
        "and paste the new Cookie.",
        title="Huawei Cloud Find Device: re-authentication required",
        notification_id=f"{DOMAIN}_reauth_{runtime.entry.entry_id}",
    )
    runtime.entry.async_start_reauth(hass)


@callback
def enabled_device_ids(hass: HomeAssistant, runtime: HuaweiFindRuntime) -> list[str]:
    """Return device ids the user has NOT disabled in the UI."""
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    active: list[str] = []
    for device_id in runtime.devices:
        device = device_reg.async_get_device(identifiers={(DOMAIN, device_id)})
        if device is None:
            active.append(device_id)
            continue
        if device.disabled_by is not None:
            continue
        entries = er.async_entries_for_device(
            entity_reg, device.id, include_disabled_entities=True
        )
        if entries and all(entry.disabled_by is not None for entry in entries):
            continue
        active.append(device_id)
    return active


async def _async_locate_with_retry(
    runtime: HuaweiFindRuntime, device: dict[str, Any]
) -> dict[str, Any]:
    """Locate a device, retrying once on transient API errors."""
    last_error: HuaweiFindError | None = None
    for attempt in range(LOCATE_RETRIES + 1):
        try:
            return await runtime.client.async_locate_and_wait(device)
        except HuaweiFindAuthError:
            raise
        except HuaweiFindError as err:
            last_error = err
            if attempt < LOCATE_RETRIES:
                await asyncio.sleep(LOCATE_RETRY_DELAY)
    raise last_error  # type: ignore[misc]


def _distance_m(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in meters."""
    radius = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def _merge_result(previous: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Keep the last known position when a refresh fails."""
    merged = dict(result)
    merged["last_attempt"] = dt_util.utcnow()
    if result.get("status") == "ok":
        merged["stale"] = False
        # Reverse geocoding can fail transiently; keep the previous address
        # as long as the device has not moved significantly.
        if (
            not merged.get("address")
            and previous.get("address")
            and previous.get("latitude") is not None
            and previous.get("longitude") is not None
        ):
            moved = _distance_m(
                previous["latitude"],
                previous["longitude"],
                merged["latitude"],
                merged["longitude"],
            )
            if moved <= ADDRESS_KEEP_RADIUS:
                merged["address"] = previous["address"]
                merged["address_stale"] = True
                return merged
        merged["address_stale"] = False
        return merged

    if previous.get("latitude") is not None and previous.get("longitude") is not None:
        for key in (
            "latitude",
            "longitude",
            "accuracy",
            "battery",
            "battery_charging",
            "screen_locked",
            "coordinate_system",
            "device_time",
            "located_at",
            "address",
        ):
            if key in previous and key not in merged:
                merged[key] = previous[key]
        merged["stale"] = True
        return merged

    merged["stale"] = False
    return merged


async def async_refresh_device(
    hass: HomeAssistant, runtime: HuaweiFindRuntime, device_id: str
) -> dict[str, Any]:
    """Trigger one locate request for a device and poll for the result."""
    device = runtime.devices.get(device_id)
    if device is None:
        return {"status": "error", "last_error": "Device not found"}

    lock = runtime.locks.setdefault(device_id, asyncio.Lock())
    if lock.locked():
        _LOGGER.debug("%s is already refreshing, skipping duplicate request", device.get("name"))
        return runtime.results.get(device_id, {})

    async with lock:
        runtime.refreshing.add(device_id)
        async_dispatcher_send(hass, signal_update(runtime.entry.entry_id, device_id))
        try:
            async with runtime.semaphore:
                result = await _async_locate_with_retry(runtime, device)
        except HuaweiFindAuthError as err:
            _LOGGER.warning("Huawei Cloud session expired: %s", err)
            result = {"status": "auth_expired", "last_error": str(err)}
            _notify_reauth(hass, runtime, str(err))
        except HuaweiFindError as err:
            _LOGGER.warning("Failed to refresh location for %s: %s", device.get("name"), err)
            result = {"status": "error", "last_error": str(err)}
        finally:
            runtime.refreshing.discard(device_id)

        result = _merge_result(runtime.results.get(device_id) or {}, result)

        if result.get("status") == "ok" and runtime.geocode_provider != GEOCODE_NONE:
            result["address"] = await async_reverse_geocode(
                hass,
                runtime.geocode_provider,
                runtime.amap_key,
                result["latitude"],
                result["longitude"],
            )

        if result.get("status") == "ok":
            runtime.reauth_requested = False
            persistent_notification.async_dismiss(
                hass, f"{DOMAIN}_reauth_{runtime.entry.entry_id}"
            )

        runtime.results[device_id] = result
        await _async_save_results(runtime)
        async_dispatcher_send(hass, signal_update(runtime.entry.entry_id, device_id))
        return result


async def _async_save_results(runtime: HuaweiFindRuntime) -> None:
    """Persist the latest results so they survive a restart."""
    if runtime.store is None:
        return
    payload: dict[str, Any] = {}
    for device_id, result in runtime.results.items():
        item = dict(result)
        for key in ("located_at", "last_attempt"):
            value = item.get(key)
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
        payload[device_id] = item
    try:
        await runtime.store.async_save(payload)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Failed to save location results")


async def async_handle_refresh_service(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler: refresh every device on the account."""
    tasks = [
        async_refresh_device(hass, runtime, device_id)
        for runtime in list(hass.data.get(DOMAIN, {}).values())
        for device_id in enabled_device_ids(hass, runtime)
    ]
    if tasks:
        await asyncio.gather(*tasks)


async def _async_auto_refresh(hass: HomeAssistant, runtime: HuaweiFindRuntime) -> None:
    """Periodic refresh of every device (driven by the scan_interval option)."""
    if runtime.reauth_requested:
        return
    tasks = [
        async_refresh_device(hass, runtime, device_id)
        for device_id in enabled_device_ids(hass, runtime)
    ]
    if tasks:
        await asyncio.gather(*tasks)


async def _async_heartbeat(hass: HomeAssistant, runtime: HuaweiFindRuntime) -> None:
    """Keep the session warm and detect an expired session early."""
    try:
        await runtime.client.async_heartbeat()
    except HuaweiFindAuthError as err:
        _LOGGER.warning("Heartbeat detected an expired session: %s", err)
        _notify_reauth(hass, runtime, str(err))
    except HuaweiFindError as err:
        _LOGGER.debug("Heartbeat error (ignored): %s", err)
    else:
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_reauth_{runtime.entry.entry_id}"
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the config entry."""
    client = HuaweiFindClient(
        entry.data.get(CONF_COOKIE, ""), cookie_listener=_make_cookie_listener(hass, entry)
    )

    try:
        all_devices = await client.async_get_devices()
    except HuaweiFindAuthError as err:
        await client.async_close()
        raise ConfigEntryAuthFailed(f"Huawei Cloud session expired: {err}") from err
    except HuaweiFindError as err:
        await client.async_close()
        raise ConfigEntryNotReady(f"Cannot connect to Huawei Cloud: {err}") from err

    selected = set(entry.data.get(CONF_DEVICES) or [])
    devices = {
        device["id"]: device
        for device in all_devices
        if not selected or device["id"] in selected
    }
    if not devices:
        await client.async_close()
        raise ConfigEntryNotReady("Huawei Cloud returned no locatable devices")

    store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.results")
    stored = await store.async_load() or {}
    results: dict[str, dict[str, Any]] = {}
    for device_id, item in stored.items():
        if not isinstance(item, dict):
            continue
        saved = dict(item)
        for key in ("located_at", "last_attempt"):
            if saved.get(key):
                parsed = dt_util.parse_datetime(saved[key])
                if parsed is not None:
                    saved[key] = parsed
        results[device_id] = saved

    options = entry.options
    runtime = HuaweiFindRuntime(
        entry=entry,
        client=client,
        devices=devices,
        results=results,
        store=store,
        geocode_provider=options.get(CONF_GEOCODE_PROVIDER, GEOCODE_NONE),
        amap_key=options.get(CONF_AMAP_KEY, ""),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH,
            partial(async_handle_refresh_service, hass),
            schema=SERVICE_SCHEMA,
        )

    async def _heartbeat(_now) -> None:
        await _async_heartbeat(hass, runtime)

    runtime.unsub_heartbeat = async_track_time_interval(
        hass, _heartbeat, HEARTBEAT_INTERVAL
    )

    interval = int(options.get(CONF_SCAN_INTERVAL) or 0)
    if interval > 0:
        async def _auto(_now) -> None:
            await _async_auto_refresh(hass, runtime)

        runtime.auto_refresh_unsub = async_track_time_interval(
            hass, _auto, timedelta(minutes=interval)
        )
        _LOGGER.info("Automatic location refresh enabled every %d minute(s)", interval)

    _LOGGER.info(
        "Huawei Cloud Find Device ready with %d device(s): %s",
        len(devices),
        ", ".join(d.get("name", "?") for d in devices.values()),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime: HuaweiFindRuntime | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if runtime is not None:
            if runtime.unsub_heartbeat is not None:
                runtime.unsub_heartbeat()
            if runtime.auto_refresh_unsub is not None:
                runtime.auto_refresh_unsub()
            await runtime.client.async_close()
    return unloaded

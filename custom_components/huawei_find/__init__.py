"""Huawei Cloud Find Device integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import HuaweiFindAuthError, HuaweiFindClient, HuaweiFindError
from .const import (
    CONF_AMAP_KEY,
    CONF_COOKIE,
    CONF_DEVICES,
    CONF_GEOCODE_PROVIDER,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    GEOCODE_AUTO,
    SERVICE_REFRESH,
    SERVICE_SET_COOKIE,
)
from .coordinator import (  # noqa: F401  (re-exported for platforms and tests)
    _async_auto_refresh,
    _async_locate_with_retry,
    _async_refresh_addresses,
    _async_save_results,
    _distance_m,
    _merge_result,
    _start_auto_refresh,
    async_refresh_device,
    async_ring_device,
    enabled_device_ids,
)
from .helpers import (  # noqa: F401  (re-exported for platforms and tests)
    HuaweiFindRuntime,
    _interval_minutes,
    _make_cookie_listener,
    _notify_reauth,
    device_identifiers,
    prune_removed_devices,
    serialize_cookies,
    signal_account,
    signal_update,
    supports_ring,
)
from .options import (  # noqa: F401  (re-exported for config flow and tests)
    _async_heartbeat,
    _check_expiry_reminder,
    apply_options,
    auth_age_days,
)
from .services import (  # noqa: F401  (registered below)
    async_handle_refresh_service,
    async_handle_set_cookie,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
]

SERVICE_SCHEMA = vol.Schema({})
SET_COOKIE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_COOKIE): str,
        vol.Optional("entry_id"): str,
    }
)

# The portal pings heartbeatCheck while a browser tab is open; doing the same
# keeps the server-side session alive and rotates CSRFToken.
HEARTBEAT_INTERVAL = timedelta(minutes=5)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the config entry."""
    client = HuaweiFindClient(
        entry.data.get(CONF_COOKIE, ""),
        cookie_listener=_make_cookie_listener(hass, entry),
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
        if device_id not in devices:
            continue
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
        geocode_provider=options.get(CONF_GEOCODE_PROVIDER, GEOCODE_AUTO),
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
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_COOKIE,
            partial(async_handle_set_cookie, hass),
            schema=SET_COOKIE_SCHEMA,
        )

    async def _heartbeat(_now) -> None:
        await _async_heartbeat(hass, runtime)

    runtime.unsub_heartbeat = async_track_time_interval(
        hass, _heartbeat, HEARTBEAT_INTERVAL
    )

    _start_auto_refresh(
        hass, runtime, _interval_minutes(options.get(CONF_SCAN_INTERVAL))
    )

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
        runtime: HuaweiFindRuntime | None = hass.data.get(DOMAIN, {}).pop(
            entry.entry_id, None
        )
        if runtime is not None:
            runtime.unloading = True
            if runtime.unsub_heartbeat is not None:
                runtime.unsub_heartbeat()
            if runtime.auto_refresh_unsub is not None:
                runtime.auto_refresh_unsub()
            pending = [task for task in runtime.tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await runtime.client.async_close()
    return unloaded

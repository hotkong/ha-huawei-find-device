"""Location refresh, ringing and periodic scheduling for one account."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import (
    HuaweiFindAuthError,
    HuaweiFindDeviceError,
    HuaweiFindError,
)
from .const import (
    ADDRESS_KEEP_RADIUS,
    DOMAIN,
    GEOCODE_NONE,
    LOCATE_RETRIES,
    LOCATE_RETRY_DELAY,
    RING_COOLDOWN,
    RING_RETRIES,
    RING_RETRY_DELAY,
)
from .geocode import async_reverse_geocode
from .helpers import (
    HuaweiFindRuntime,
    _notify_reauth,
    device_identifiers,
    signal_account,
    signal_update,
    supports_ring,
)

_LOGGER = logging.getLogger(__name__)


async def _async_locate_with_retry(
    runtime: HuaweiFindRuntime, device: dict[str, Any]
) -> dict[str, Any]:
    """Locate a device, retrying once on transient API errors."""
    last_error: HuaweiFindError | None = None
    for attempt in range(LOCATE_RETRIES + 1):
        try:
            return await runtime.client.async_locate_and_wait(device)
        except (HuaweiFindAuthError, HuaweiFindDeviceError):
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
        kept_address = False
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
                if key == "address":
                    kept_address = True
        merged["stale"] = True
        if kept_address:
            # The address is inherited, not freshly resolved
            merged["address_stale"] = True
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
        _LOGGER.debug(
            "%s is already refreshing, skipping duplicate request", device.get("name")
        )
        return runtime.results.get(device_id, {})

    task = asyncio.current_task()
    if task is not None:
        runtime.tasks.add(task)
    try:
        async with lock:
            runtime.refreshing.add(device_id)
            async_dispatcher_send(
                hass, signal_update(runtime.entry.entry_id, device_id)
            )
            async_dispatcher_send(hass, signal_account(runtime.entry.entry_id))
            try:
                async with runtime.semaphore:
                    result = await _async_locate_with_retry(runtime, device)
            except HuaweiFindAuthError as err:
                _LOGGER.warning("Huawei Cloud session expired: %s", err)
                result = {"status": "auth_expired", "last_error": str(err)}
                _notify_reauth(hass, runtime, str(err))
            except HuaweiFindDeviceError as err:
                _LOGGER.debug("Device %s is not reachable: %s", device.get("name"), err)
                result = {"status": "error", "last_error": str(err)}
            except HuaweiFindError as err:
                _LOGGER.warning(
                    "Failed to refresh location for %s: %s", device.get("name"), err
                )
                result = {"status": "error", "last_error": str(err)}
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error refreshing %s", device.get("name"))
                result = {"status": "error", "last_error": f"Unexpected error: {err}"}
            finally:
                runtime.refreshing.discard(device_id)

            if result.get("status") == "ok" and runtime.geocode_provider != GEOCODE_NONE:
                address = await async_reverse_geocode(
                    hass,
                    runtime.geocode_provider,
                    runtime.amap_key,
                    result["latitude"],
                    result["longitude"],
                )
                # Only set when successful: a None must not wipe the merged address
                if address:
                    result["address"] = address

            result = _merge_result(runtime.results.get(device_id) or {}, result)

            if result.get("status") == "ok":
                runtime.reauth_requested = False
                persistent_notification.async_dismiss(
                    hass, f"{DOMAIN}_reauth_{runtime.entry.entry_id}"
                )

            if runtime.unloading:
                # Entry is being unloaded; never write through a stale runtime
                return result

            runtime.results[device_id] = result
            await _async_save_results(runtime)
            async_dispatcher_send(
                hass, signal_update(runtime.entry.entry_id, device_id)
            )
            async_dispatcher_send(hass, signal_account(runtime.entry.entry_id))
            return result
    finally:
        if task is not None:
            runtime.tasks.discard(task)


async def async_ring_device(
    hass: HomeAssistant, runtime: HuaweiFindRuntime, device_id: str
) -> dict[str, Any]:
    """Ask one device to play its alarm sound."""
    device = runtime.devices.get(device_id)
    if device is None:
        return {"ok": False, "error": "Device not found"}
    if not supports_ring(device):
        return {
            "ok": False,
            "error": "Ringing is only supported for phones on this account",
        }
    elapsed = time.monotonic() - runtime.last_ring.get(device_id, 0.0)
    if elapsed < RING_COOLDOWN:
        wait = int(RING_COOLDOWN - elapsed) + 1
        return {"ok": False, "error": f"Please wait {wait}s before ringing again"}

    for attempt in range(RING_RETRIES + 1):
        try:
            await runtime.client.async_ring(device)
            break
        except HuaweiFindAuthError as err:
            _notify_reauth(hass, runtime, str(err))
            runtime.ring_errors[device_id] = f"auth: {err}"
            _LOGGER.warning("Ring failed for %s: session expired: %s", device.get("name"), err)
            return {"ok": False, "error": str(err)}
        except HuaweiFindDeviceError as err:
            runtime.ring_errors[device_id] = f"device: {err}"
            _LOGGER.warning("Ring failed for %s: %s", device.get("name"), err)
            return {"ok": False, "error": str(err)}
        except HuaweiFindError as err:
            push_failed = "send push failed" in str(err).lower()
            if push_failed and attempt < RING_RETRIES:
                _LOGGER.info(
                    "Ring push failed for %s, retrying in %ss",
                    device.get("name"),
                    RING_RETRY_DELAY,
                )
                await asyncio.sleep(RING_RETRY_DELAY)
                continue
            runtime.ring_errors[device_id] = f"api: {err}"
            _LOGGER.warning("Ring failed for %s: %s", device.get("name"), err)
            return {"ok": False, "error": str(err)}

    runtime.ring_errors.pop(device_id, None)
    runtime.last_ring[device_id] = time.monotonic()
    runtime.ring_results[device_id] = dt_util.utcnow()
    async_dispatcher_send(hass, signal_update(runtime.entry.entry_id, device_id))
    _LOGGER.info("Ring requested for %s", device.get("name"))
    return {"ok": True}


async def _async_save_results(runtime: HuaweiFindRuntime) -> None:
    """Persist the latest results so they survive a restart."""
    if runtime.store is None:
        return
    try:
        # Build the snapshot inside the lock so a queued save cannot write
        # an older view over a newer one.
        async with runtime.save_lock:
            payload: dict[str, Any] = {}
            for device_id, result in runtime.results.items():
                item = dict(result)
                for key in ("located_at", "last_attempt"):
                    value = item.get(key)
                    if hasattr(value, "isoformat"):
                        item[key] = value.isoformat()
                payload[device_id] = item
            await runtime.store.async_save(payload)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Failed to save location results")


@callback
def enabled_device_ids(hass: HomeAssistant, runtime: HuaweiFindRuntime) -> list[str]:
    """Return device ids the user has NOT disabled in the UI."""
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    active: list[str] = []
    for device_id in runtime.devices:
        device = device_reg.async_get_device(
            identifiers=device_identifiers(runtime.entry.entry_id, device_id)
        )
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


@callback
def _start_auto_refresh(
    hass: HomeAssistant, runtime: HuaweiFindRuntime, interval: int
) -> None:
    """(Re)schedule the periodic refresh without reloading the entry."""
    if runtime.auto_refresh_unsub is not None:
        runtime.auto_refresh_unsub()
        runtime.auto_refresh_unsub = None
    if interval <= 0:
        return

    async def _auto(_now) -> None:
        await _async_auto_refresh(hass, runtime)

    runtime.auto_refresh_unsub = async_track_time_interval(
        hass, _auto, timedelta(minutes=interval)
    )
    _LOGGER.info("Automatic location refresh enabled every %d minute(s)", interval)


async def _async_refresh_addresses(
    hass: HomeAssistant, runtime: HuaweiFindRuntime
) -> None:
    """Re-resolve addresses for known coordinates (no phone wake-up)."""
    if runtime.geocode_provider == GEOCODE_NONE:
        for device_id, result in list(runtime.results.items()):
            result.pop("address", None)
            result.pop("address_stale", None)
            async_dispatcher_send(
                hass, signal_update(runtime.entry.entry_id, device_id)
            )
        await _async_save_results(runtime)
        return

    for device_id, result in list(runtime.results.items()):
        if runtime.unloading:
            return
        latitude = result.get("latitude")
        longitude = result.get("longitude")
        if latitude is None or longitude is None:
            continue
        address = await async_reverse_geocode(
            hass,
            runtime.geocode_provider,
            runtime.amap_key,
            latitude,
            longitude,
        )
        current = runtime.results.get(device_id)
        if current is not result:
            # A refresh replaced this entry meanwhile; it carries its own address
            continue
        if address:
            result["address"] = address
            result["address_stale"] = False
        elif result.get("address"):
            result["address_stale"] = True
        else:
            result.pop("address", None)
        async_dispatcher_send(hass, signal_update(runtime.entry.entry_id, device_id))

    await _async_save_results(runtime)

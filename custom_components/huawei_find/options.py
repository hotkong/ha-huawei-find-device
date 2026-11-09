"""Options hot-apply, session age and the heartbeat/expiry reminders."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .api import HuaweiFindAuthError, HuaweiFindError
from .const import (
    CONF_AMAP_KEY,
    CONF_AUTH_TIME,
    CONF_GEOCODE_PROVIDER,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    EXPIRY_WARNING_DAYS,
    GEOCODE_AUTO,
)
from .coordinator import _async_refresh_addresses, _start_auto_refresh
from .helpers import (
    HuaweiFindRuntime,
    _interval_minutes,
    _notify_reauth,
    signal_account,
)

_LOGGER = logging.getLogger(__name__)


def auth_age_days(entry: ConfigEntry) -> float | None:
    """Days since the current cookie was pasted."""
    raw = entry.data.get(CONF_AUTH_TIME)
    if not raw:
        return None
    parsed = dt_util.parse_datetime(raw)
    if parsed is None:
        return None
    return (dt_util.utcnow() - parsed).total_seconds() / 86400


def _check_expiry_reminder(
    hass: HomeAssistant, runtime: HuaweiFindRuntime
) -> None:
    """Warn once when the cookie is getting close to the observed lifetime."""
    days = auth_age_days(runtime.entry)
    if days is None:
        return
    notification_id = f"{DOMAIN}_expiry_{runtime.entry.entry_id}"
    if days < EXPIRY_WARNING_DAYS:
        persistent_notification.async_dismiss(hass, notification_id)
        runtime.expiry_warned = False
        return
    if runtime.expiry_warned:
        return
    runtime.expiry_warned = True
    persistent_notification.async_create(
        hass,
        f"The Huawei Cloud cookie has been in use for {days:.0f} days. "
        "The observed session lifetime is around a week, so update it now "
        "to avoid a gap in location updates.\n\n"
        "Settings > Devices & Services > Huawei Cloud Find Device > "
        "Re-authenticate, or call huawei_find.set_cookie.",
        title="Huawei Cloud Find Device: cookie is getting old",
        notification_id=notification_id,
    )


async def _async_heartbeat(hass: HomeAssistant, runtime: HuaweiFindRuntime) -> None:
    """Keep the session warm and detect an expired session early."""
    try:
        await runtime.client.async_heartbeat()
    except HuaweiFindAuthError as err:
        runtime.heartbeat_failures += 1
        runtime.heartbeat_ok = False
        _LOGGER.error("Heartbeat: session expired: %s", err)
        _notify_reauth(hass, runtime, str(err))
    except HuaweiFindError as err:
        runtime.heartbeat_failures += 1
        runtime.heartbeat_ok = False
        _LOGGER.warning("Heartbeat failed: %s", err)
    else:
        runtime.heartbeat_ok = True
        previous_beat = runtime.last_heartbeat
        now = dt_util.utcnow()
        runtime.last_heartbeat = now
        if previous_beat is None or (now - previous_beat) > timedelta(hours=1):
            _LOGGER.info(
                "Heartbeat OK (auth age %.1f days)", auth_age_days(runtime.entry) or 0
            )
        else:
            _LOGGER.debug("Heartbeat OK")
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_reauth_{runtime.entry.entry_id}"
        )
        _check_expiry_reminder(hass, runtime)
    finally:
        async_dispatcher_send(hass, signal_account(runtime.entry.entry_id))


@callback
def apply_options(
    hass: HomeAssistant, entry: ConfigEntry, options: dict[str, Any]
) -> bool:
    """Apply new options to the running entry without unloading it."""
    runtime: HuaweiFindRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return False

    previous_provider = runtime.geocode_provider
    previous_key = runtime.amap_key
    runtime.geocode_provider = options.get(CONF_GEOCODE_PROVIDER, GEOCODE_AUTO)
    runtime.amap_key = options.get(CONF_AMAP_KEY, "")

    if (
        runtime.geocode_provider != previous_provider
        or runtime.amap_key != previous_key
    ):
        task = hass.async_create_task(_async_refresh_addresses(hass, runtime))
        runtime.tasks.add(task)
        task.add_done_callback(runtime.tasks.discard)

    _start_auto_refresh(
        hass, runtime, _interval_minutes(options.get(CONF_SCAN_INTERVAL))
    )
    _LOGGER.info(
        "Options applied: provider=%s, interval=%s",
        runtime.geocode_provider,
        _interval_minutes(options.get(CONF_SCAN_INTERVAL)),
    )
    return True

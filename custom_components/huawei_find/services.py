"""Service handlers exposed by the integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .api import HuaweiFindError
from .const import CONF_AUTH_TIME, CONF_COOKIE, DOMAIN
from .coordinator import async_refresh_device, enabled_device_ids

_LOGGER = logging.getLogger(__name__)


async def async_handle_set_cookie(hass: HomeAssistant, call: ServiceCall) -> None:
    """Update the session cookie without going through the UI."""
    cookie = (call.data.get(CONF_COOKIE) or "").strip()
    target = call.data.get("entry_id")
    runtimes = list(hass.data.get(DOMAIN, {}).values())
    if target:
        runtimes = [r for r in runtimes if r.entry.entry_id == target]
        if not runtimes:
            raise HomeAssistantError(f"Unknown entry_id: {target}")
    elif len(runtimes) > 1:
        raise HomeAssistantError(
            "Multiple Huawei accounts are configured; pass entry_id to choose one"
        )

    for runtime in runtimes:
        try:
            runtime.client.set_cookie(cookie)
        except HuaweiFindError as err:
            _LOGGER.error("set_cookie rejected: %s", err)
            continue
        runtime.reauth_requested = False
        runtime.expiry_warned = False
        hass.config_entries.async_update_entry(
            runtime.entry,
            data={
                **runtime.entry.data,
                CONF_COOKIE: cookie,
                CONF_AUTH_TIME: dt_util.utcnow().isoformat(),
            },
        )
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_reauth_{runtime.entry.entry_id}"
        )
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_expiry_{runtime.entry.entry_id}"
        )
        _LOGGER.info("Cookie updated via service for %s", runtime.entry.title)


async def async_handle_refresh_service(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler: refresh every device on every configured account."""
    tasks = [
        async_refresh_device(hass, runtime, device_id)
        for runtime in list(hass.data.get(DOMAIN, {}).values())
        for device_id in enabled_device_ids(hass, runtime)
    ]
    if tasks:
        await asyncio.gather(*tasks)

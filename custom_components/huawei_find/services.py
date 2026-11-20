"""Service handlers exposed by the integration."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .api import (
    HuaweiFindAuthError,
    HuaweiFindClient,
    HuaweiFindError,
    cookie_matches_entry,
    cookie_user_id,
)
from .browser_login import (
    BrowserLoginError,
    async_browser_login,
)
from .const import CONF_AUTH_TIME, CONF_COOKIE, DOMAIN
from .coordinator import async_refresh_device, enabled_device_ids
from .helpers import abort_reauth_flows

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
        abort_reauth_flows(hass, runtime.entry)
        _LOGGER.info("Cookie updated via service for %s", runtime.entry.title)


async def async_handle_refresh_service(hass: HomeAssistant, call: ServiceCall) -> None:
    """Refresh all devices, or one HA device when device_id is given."""
    target_entry = call.data.get("entry_id")
    target_device = call.data.get("device_id")

    if target_device:
        from homeassistant.helpers import device_registry as dr

        registry = dr.async_get(hass)
        device = registry.async_get(target_device)
        if device is None:
            raise HomeAssistantError(f"Unknown device_id: {target_device}")
        targets: list[tuple[str, str]] = []
        for domain, identifier in device.identifiers:
            if domain != DOMAIN or ":" not in identifier:
                continue
            entry_id, huawei_id = identifier.split(":", 1)
            targets.append((entry_id, huawei_id))
        if not targets:
            raise HomeAssistantError(
                f"{target_device}: not a {DOMAIN} device"
            )
        for entry_id, huawei_id in targets:
            if target_entry and entry_id != target_entry:
                raise HomeAssistantError(
                    f"{target_device}: device does not belong to entry {target_entry}"
                )
            runtime = hass.data.get(DOMAIN, {}).get(entry_id)
            if runtime is None or huawei_id not in runtime.devices:
                raise HomeAssistantError(
                    f"{target_device}: device is not loaded for this account"
                )
            await async_refresh_device(hass, runtime, huawei_id)
        return

    runtimes = list(hass.data.get(DOMAIN, {}).values())
    if target_entry:
        runtimes = [r for r in runtimes if r.entry.entry_id == target_entry]
        if not runtimes:
            raise HomeAssistantError(f"Unknown entry_id: {target_entry}")
    tasks = [
        async_refresh_device(hass, runtime, device_id)
        for runtime in runtimes
        for device_id in enabled_device_ids(hass, runtime)
    ]
    if tasks:
        await asyncio.gather(*tasks)


async def async_handle_refresh_cookie(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    """Test the browser login, verify the cookie, and optionally store it."""
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
    if not runtimes:
        raise HomeAssistantError("No Huawei account is configured")
    runtime = runtimes[0]

    if not (
        runtime.browser_url
        and runtime.browser_username
        and runtime.browser_password
    ):
        raise HomeAssistantError(
            f"{runtime.entry.title}: browser login is not configured "
            "(set browser URL and Huawei credentials in options)"
        )
    if runtime.login_lock.locked():
        raise HomeAssistantError(
            f"{runtime.entry.title}: another browser login is already running"
        )

    update = bool(call.data.get("update", True))
    async with runtime.login_lock:
        runtime.last_login_attempt = time.monotonic()
        try:
            cookie = await async_browser_login(
                runtime.browser_url,
                runtime.browser_username,
                runtime.browser_password,
                runtime.browser_token,
            )
        except BrowserLoginError as err:
            runtime.login_error = str(err)
            raise HomeAssistantError(
                f"{runtime.entry.title}: browser login failed: {err}"
            ) from err

        if not cookie_matches_entry(cookie, runtime.entry.unique_id):
            runtime.login_error = "browser login returned a different Huawei account"
            raise HomeAssistantError(
                f"{runtime.entry.title}: browser login returned a different Huawei account"
            )

        try:
            client = HuaweiFindClient(cookie)
        except HuaweiFindError as err:
            runtime.login_error = f"cookie rejected: {err}"
            raise HomeAssistantError(
                f"{runtime.entry.title}: cookie rejected by the Huawei client: {err}"
            ) from err
        try:
            devices = await client.async_get_devices()
        except HuaweiFindAuthError as err:
            runtime.login_error = f"cookie rejected by Huawei API: {err}"
            raise HomeAssistantError(
                f"{runtime.entry.title}: cookie rejected by Huawei API: {err}"
            ) from err
        except HuaweiFindError as err:
            runtime.login_error = f"cannot verify cookie: {err}"
            raise HomeAssistantError(
                f"{runtime.entry.title}: cannot verify the new cookie: {err}"
            ) from err
        finally:
            await client.async_close()

        if not devices:
            runtime.login_error = "Huawei Cloud returned no devices for this cookie"
            raise HomeAssistantError(
                f"{runtime.entry.title}: the new cookie returned no devices"
            )

        if update:
            try:
                runtime.client.set_cookie(cookie)
            except HuaweiFindError as err:
                runtime.login_error = f"cookie rejected: {err}"
                raise HomeAssistantError(
                    f"{runtime.entry.title}: cookie rejected: {err}"
                ) from err
            runtime.reauth_requested = False
            runtime.expiry_warned = False
            runtime.login_error = None
            runtime.last_login_success = dt_util.utcnow()
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
            abort_reauth_flows(hass, runtime.entry)
            _LOGGER.info(
                "Cookie refreshed via refresh_cookie for %s", runtime.entry.title
            )

        user_id = cookie_user_id(cookie) or runtime.client.user_id or ""
        return {
            "success": True,
            "updated": update,
            "cookie_length": len(cookie),
            "user_id_tail": user_id[-4:],
            "device_count": len(devices),
        }

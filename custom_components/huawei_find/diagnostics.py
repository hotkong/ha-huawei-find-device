"""Diagnostics support: a redacted snapshot users can attach to an issue.

Home Assistant exposes this as "Download diagnostics" on the integration page.
Never include cookies, passwords, tokens, addresses or precise coordinates.
"""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .helpers import HuaweiFindRuntime

TO_REDACT = {
    "cookie",
    "password",
    "browser_token",
    "token",
    "user_id",
    "unique_id",
}

_COORD_PRECISION = 2  # ~1 km, enough to debug without exposing a home address


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def sanitize_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip addresses and round coordinates; keep debugging-relevant fields."""
    if not result:
        return None
    latitude = result.get("latitude")
    longitude = result.get("longitude")
    payload: dict[str, Any] = {
        "status": result.get("status"),
        "stale": result.get("stale"),
        "last_error": result.get("last_error"),
        "accuracy": result.get("accuracy"),
        "coordinate_system": result.get("coordinate_system"),
        "device_time": result.get("device_time"),
        "result_time": result.get("result_time"),
        "result_stale": result.get("result_stale"),
        "located_at": _iso(result.get("located_at")),
        "last_attempt": _iso(result.get("last_attempt")),
        "battery": result.get("battery"),
        "battery_stale": result.get("battery_stale"),
        "batteries": result.get("batteries"),
    }
    if latitude is not None and longitude is not None:
        payload["latitude"] = round(float(latitude), _COORD_PRECISION)
        payload["longitude"] = round(float(longitude), _COORD_PRECISION)
    return {key: value for key, value in payload.items() if value is not None}


def device_summary(device: dict[str, Any]) -> dict[str, Any]:
    """Describe a device without exposing its raw Huawei id."""
    return {
        "id_hash": _digest(str(device.get("id", ""))),
        "name": device.get("name"),
        "model": device.get("model"),
        "per_device_type": device.get("per_device_type"),
        "components": device.get("components"),
        "capability": device.get("capability"),
        "is_shared": device.get("is_shared"),
    }


def _options_summary(runtime: HuaweiFindRuntime) -> dict[str, Any]:
    options = runtime.entry.options
    return {
        "geocode_provider": runtime.geocode_provider,
        "scan_interval": options.get("scan_interval"),
        "amap_key_configured": bool(runtime.amap_key),
        "browser_configured": bool(runtime.browser_url),
        "browser_token_configured": bool(runtime.browser_token),
        "huawei_credentials_configured": bool(
            runtime.browser_username and runtime.browser_password
        ),
    }


def _session_summary(runtime: HuaweiFindRuntime) -> dict[str, Any]:
    auth_time = runtime.entry.data.get("auth_time")
    days = None
    if auth_time:
        parsed = dt_util.parse_datetime(auth_time)
        if parsed is not None:
            days = round((dt_util.utcnow() - parsed).total_seconds() / 86400, 1)
    cookie = runtime.entry.data.get("cookie", "")
    return {
        "auth_time": auth_time,
        "days_since_auth": days,
        "cookie_length": len(cookie),
        "cookie_has_csrf": "csrftoken=" in cookie.lower(),
        "cookie_has_user": "user_id=" in cookie.lower(),
        "heartbeat_ok": runtime.heartbeat_ok,
        "heartbeat_failures": runtime.heartbeat_failures,
        "last_heartbeat": _iso(runtime.last_heartbeat),
        "reauth_requested": runtime.reauth_requested,
        "expiry_warned": runtime.expiry_warned,
        "last_login_error": runtime.login_error,
        "last_login_success": _iso(runtime.last_login_success),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a redacted snapshot of one account entry."""
    runtime: HuaweiFindRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    integration = await async_get_integration(hass, DOMAIN)

    payload: dict[str, Any] = {
        "entry": {
            "entry_id_hash": _digest(entry.entry_id),
            "title": entry.title,
            "state": str(getattr(entry, "state", "unknown")),
            "integration_version": getattr(integration, "version", None),
            "ha_version": HA_VERSION,
        },
        "options": _options_summary(runtime) if runtime else {},
        "session": _session_summary(runtime) if runtime else {},
        "devices": [],
    }

    if runtime is not None:
        for device_id, device in runtime.devices.items():
            item = device_summary(device)
            item["result"] = sanitize_result(runtime.results.get(device_id))
            payload["devices"].append(item)
        payload["refreshing"] = sorted(
            _digest(device_id) for device_id in runtime.refreshing
        )
        payload["ring_errors"] = {
            _digest(device_id): error
            for device_id, error in runtime.ring_errors.items()
        }

    return async_redact_data(payload, TO_REDACT)

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
from homeassistant.helpers.entity import DeviceInfo
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
    """Own phones/wearables/tags, plus devices that advertise the bell capability."""
    if device.get("is_shared"):
        return False
    if str(device.get("per_device_type") or "") in {"0", "2", "3"}:
        return True
    return "bell" in (device.get("capability") or [])


def supports_stop_ring(device: dict[str, Any]) -> bool:
    """Only wearables, Tags and bell-capable earbuds have a remote stop."""
    if device.get("is_shared"):
        return False
    if str(device.get("per_device_type") or "") in {"2", "3"}:
        return True
    return "bell" in (device.get("capability") or [])


def build_device_info(entry_id: str, device: dict[str, Any]) -> DeviceInfo:
    """Build HA device metadata from fields the Huawei API actually returns."""
    category = str(device.get("category") or "").lower()
    category_label = {
        "phone": "Huawei phone",
        "watch": "Huawei watch",
        "band": "Huawei band",
        "tag": "HUAWEI Tag",
    }.get(category, category or None)
    model = device.get("model") or category_label
    info = DeviceInfo(
        identifiers=device_identifiers(entry_id, device["id"]),
        name=device.get("name") or "Huawei device",
        manufacturer="Huawei",
        model=model,
        configuration_url="https://cloud.huawei.com/webFindPhone.html",
    )
    if device.get("firmware"):
        info["sw_version"] = device["firmware"]
    if device.get("serial_number"):
        info["serial_number"] = device["serial_number"]
    return info


def signal_update(entry_id: str, device_id: str) -> str:
    """Dispatcher signal for one device of one entry."""
    return f"{DOMAIN}_update_{entry_id}_{device_id}"


def supports_charging(device: dict[str, Any]) -> bool:
    """Only phones report a charging flag; wearables and earbuds do not."""
    per_type = str(device.get("per_device_type") or "")
    return per_type in {"0", ""}


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


_REAUTH_NOTICE: dict[str, dict[str, str]] = {
    "zh": {
        "title": "华为云需要重新授权",
        "expired": "华为云会话已过期（{reason}）。",
        "failed": "\n\n自动重登失败：{error}",
        "none": "\n\n未配置自动重登。",
        "guide": (
            "\n\n重新验证：设置 → 设备与服务 → Huawei Cloud Find Device → 重新验证。\n"
            "· 华为账号密码：免抓 Cookie；会结束该账号的其他网页会话\n"
            "· 粘贴新 Cookie：最快；需登录 cloud.huawei.com 复制请求头\n"
            "· browserless：需已配置浏览器服务"
        ),
    },
    "en": {
        "title": "Huawei Cloud: re-authentication required",
        "expired": "The Huawei Cloud session has expired ({reason}).",
        "failed": "\n\nAutomatic re-login failed: {error}",
        "none": "\n\nNo automatic re-login is configured.",
        "guide": (
            "\n\nRe-authenticate: Settings → Devices & Services → "
            "Huawei Cloud Find Device.\n"
            "· Huawei account and password: no cookie hunting; "
            "ends other web sessions\n"
            "· Paste a new Cookie: fastest; copy the request header from "
            "cloud.huawei.com\n"
            "· browserless: needs a configured browser service"
        ),
    },
}


def reauth_notice_text(
    hass: HomeAssistant, reason: str, login_error: str, has_fallback: bool
) -> tuple[str, str]:
    """Localised title and body for the expired-session notification."""
    language = str(getattr(hass.config, "language", "") or "").lower()
    texts = _REAUTH_NOTICE["zh" if language.startswith("zh") else "en"]
    if login_error:
        detail = texts["failed"].format(error=login_error)
    elif not has_fallback:
        detail = texts["none"]
    else:
        detail = ""
    message = texts["expired"].format(reason=reason) + detail + texts["guide"]
    return texts["title"], message


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
    title, message = reauth_notice_text(
        hass, reason, runtime.login_error or "", has_fallback
    )
    persistent_notification.async_create(
        hass,
        message,
        title=title,
        notification_id=f"{DOMAIN}_reauth_{runtime.entry.entry_id}",
    )
    runtime.entry.async_start_reauth(hass)

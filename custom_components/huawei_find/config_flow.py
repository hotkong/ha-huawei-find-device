"""Config flow: paste a Huawei Cloud cookie, then select devices."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import HuaweiFindAuthError, HuaweiFindClient, HuaweiFindError
from .const import (
    CONF_AMAP_KEY,
    CONF_AUTH_TIME,
    CONF_COOKIE,
    CONF_DEVICES,
    CONF_GEOCODE_PROVIDER,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GEOCODE_AMAP,
    GEOCODE_AUTO,
    GEOCODE_HUAWEI,
    GEOCODE_NONE,
    GEOCODE_OSM,
    MAX_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

ENTRY_TITLE = "Huawei Cloud Find Device"


def _scan_interval_value(value: Any) -> int:
    """Clamp a scan_interval form value into [0, MAX_SCAN_INTERVAL]."""
    if value is None or value == "":
        return DEFAULT_SCAN_INTERVAL
    try:
        return max(0, min(MAX_SCAN_INTERVAL, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_SCAN_INTERVAL


COOKIE_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
)
STEP_USER_SCHEMA = vol.Schema({vol.Required(CONF_COOKIE): COOKIE_SELECTOR})

# Matches a "Cookie:" request header (value may be followed by more headers).
_COOKIE_HEADER_RE = re.compile(
    r"cookie:\s*([^\r\n]+)", re.IGNORECASE
)
# Matches the -b/--cookie argument of a curl command. The value may be a
# single token or a full "k=v; k=v" string, so allow spaces within quotes.
_CURL_COOKIE_RE = re.compile(
    r"""-b\s+(?:"([^"]+)"|'([^']+)'|(\S+))""", re.IGNORECASE
)
# Matches PowerShell's New-Object System.Net.Cookie("name", "value", ...) calls.
_PS_COOKIE_RE = re.compile(
    r"""New-Object\s+System\.Net\.Cookie\(\s*"?([A-Za-z0-9_]+)"?\s*,\s*"?([^",]*)""",
)


def _extract_cookie(text: str) -> str:
    """Extract a Huawei Cloud cookie from pasted content.

    Users often paste an entire ``locate`` request (devtools "Copy as cURL",
    "Copy request headers", or a PowerShell snippet) rather than just the
    Cookie header. Accept all of these and return the bare cookie string so
    the flow can validate it, falling back to the raw text when it already
    looks like a cookie.
    """
    # Windows curl escapes quotes as ^"; normalise before matching.
    text = text.replace('^"', '"').replace('"^', '"').replace("\r\n", "\n")

    # 1. A "Cookie:" request header.
    match = _COOKIE_HEADER_RE.search(text)
    if match and "HWWAFSESID" in match.group(1):
        return match.group(1).strip()

    # 2. curl -b cookie arguments. Huawei rotates tokens between requests,
    #    so prefer the last one that still carries the session id.
    for groups in reversed(_CURL_COOKIE_RE.findall(text)):
        candidate = next((g for g in groups if g), "")
        if "HWWAFSESID" in candidate:
            return candidate.strip()

    # 3. PowerShell Cookies.Add blocks, grouped per request; the last group
    #    holds the freshest tokens.
    pairs = _PS_COOKIE_RE.findall(text)
    if pairs and any(name == "HWWAFSESID" for name, _ in pairs):
        blocks: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        for name, value in pairs:
            if name == "HWWAFSESID" and current:
                blocks.append(current)
                current = []
            current.append((name, value))
        if current:
            blocks.append(current)
        return "; ".join(f"{name}={value}" for name, value in blocks[-1])

    # 4. Already a bare cookie string.
    if "HWWAFSESID=" in text and ";" in text:
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("cookie:"):
                line = line.split(":", 1)[1].strip()
            if "HWWAFSESID=" in line and ";" in line:
                return line

    return ""


class HuaweiFindConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Huawei Cloud Find Device entry or re-authorize it."""

    VERSION = 1

    def __init__(self) -> None:
        self._cookie = ""
        self._devices: list[dict[str, Any]] = []
        self._user_id = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HuaweiFindOptionsFlow:
        return HuaweiFindOptionsFlow()

    # -- initial setup ------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cookie = (user_input.get(CONF_COOKIE) or "").strip()
            extracted = _extract_cookie(cookie)
            if extracted:
                cookie = extracted
            try:
                client = HuaweiFindClient(cookie)
            except HuaweiFindError:
                errors["base"] = "invalid_auth"
                client = None
            if client is None:
                return self.async_show_form(
                    step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
                )
            try:
                devices = await client.async_get_devices()
            except HuaweiFindAuthError:
                errors["base"] = "invalid_auth"
            except HuaweiFindError as err:
                _LOGGER.warning("Cannot connect to Huawei Cloud: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    if client.user_id:
                        self._user_id = client.user_id
                        await self.async_set_unique_id(f"huawei_{client.user_id}")
                        self._abort_if_unique_id_configured()
                    self._cookie = cookie
                    self._devices = devices
                    return await self.async_step_devices()
            finally:
                await client.async_close()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            selected = user_input.get(CONF_DEVICES) or [
                device["id"] for device in self._devices
            ]
            return self.async_create_entry(
                title=(
                    f"{ENTRY_TITLE} ({self._user_id[-4:]})"
                    if self._user_id
                    else ENTRY_TITLE
                ),
                data={
                    CONF_COOKIE: self._cookie,
                    CONF_DEVICES: selected,
                    CONF_AUTH_TIME: dt_util.utcnow().isoformat(),
                },
                options={
                    CONF_SCAN_INTERVAL: _scan_interval_value(
                        user_input.get(CONF_SCAN_INTERVAL)
                    )
                },
            )

        options = [
            SelectOptionDict(
                value=device["id"],
                label=(
                    f"{device['name']} "
                    f"({'shared' if device.get('is_shared') else 'own'})"
                ),
            )
            for device in self._devices
        ]
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICES, default=[d["id"] for d in self._devices]
                    ): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=True)
                    ),
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=MAX_SCAN_INTERVAL,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                }
            ),
        )

    # -- re-authorization ---------------------------------------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reauth_failed")

        if user_input is not None:
            cookie = (user_input.get(CONF_COOKIE) or "").strip()
            extracted = _extract_cookie(cookie)
            if extracted:
                cookie = extracted
            try:
                client = HuaweiFindClient(cookie)
            except HuaweiFindError:
                errors["base"] = "invalid_auth"
                client = None
            if client is None:
                return self.async_show_form(
                    step_id="reauth_confirm",
                    data_schema=STEP_USER_SCHEMA,
                    errors=errors,
                )
            try:
                devices = await client.async_get_devices()
            except HuaweiFindAuthError:
                errors["base"] = "invalid_auth"
            except HuaweiFindError:
                errors["base"] = "cannot_connect"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={
                            **entry.data,
                            CONF_COOKIE: cookie,
                            CONF_AUTH_TIME: dt_util.utcnow().isoformat(),
                        },
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                    return self.async_abort(reason="reauth_successful")
            finally:
                await client.async_close()

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=STEP_USER_SCHEMA, errors=errors
        )


class HuaweiFindOptionsFlow(OptionsFlow):
    """Single page: address source, refresh interval and tracked devices."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        errors: dict[str, str] = {}

        devices = getattr(self, "_available_devices", None)
        if devices is None and not getattr(self, "_devices_failed", False):
            client = HuaweiFindClient(entry.data.get(CONF_COOKIE, ""))
            try:
                devices = await client.async_get_devices()
            except (HuaweiFindAuthError, HuaweiFindError):
                devices = None
                self._devices_failed = True
            finally:
                await client.async_close()
            self._available_devices = devices
        if getattr(self, "_devices_failed", False):
            errors["base"] = "devices_unavailable"

        if user_input is not None:
            selected = user_input.get(CONF_DEVICES)
            if selected is not None and not selected:
                errors["base"] = "no_devices"
            else:
                options = {
                    CONF_GEOCODE_PROVIDER: user_input.get(
                        CONF_GEOCODE_PROVIDER, GEOCODE_AUTO
                    ),
                    CONF_AMAP_KEY: (user_input.get(CONF_AMAP_KEY) or "").strip(),
                    CONF_SCAN_INTERVAL: _scan_interval_value(
                        user_input.get(CONF_SCAN_INTERVAL)
                    ),
                }
                self.hass.config_entries.async_update_entry(entry, options=options)

                from . import apply_options, prune_removed_devices

                if selected is not None:
                    prune_removed_devices(self.hass, entry, set(selected))
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_DEVICES: selected}
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                elif not apply_options(self.hass, entry, options):
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                return self.async_create_entry(title="", data=options)

        current = entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_GEOCODE_PROVIDER,
                default=current.get(CONF_GEOCODE_PROVIDER, GEOCODE_AUTO),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=GEOCODE_AUTO,
                            label="Auto — Huawei → AMap → OSM (recommended)",
                        ),
                        SelectOptionDict(
                            value=GEOCODE_HUAWEI,
                            label="Huawei Map (no key needed)",
                        ),
                        SelectOptionDict(
                            value=GEOCODE_AMAP,
                            label="AMap / Gaode (requires a key)",
                        ),
                        SelectOptionDict(
                            value=GEOCODE_OSM,
                            label="OpenStreetMap (no key)",
                        ),
                        SelectOptionDict(
                            value=GEOCODE_NONE,
                            label="Disabled — coordinates only",
                        ),
                    ]
                )
            ),
            vol.Optional(
                CONF_AMAP_KEY, default=current.get(CONF_AMAP_KEY, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=MAX_SCAN_INTERVAL,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
        }

        if devices is not None:
            current_devices = list(
                entry.data.get(CONF_DEVICES) or [d["id"] for d in devices]
            )
            schema[vol.Required(CONF_DEVICES, default=current_devices)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=device["id"],
                            label=(
                                f"{device['name']} "
                                f"({'shared' if device.get('is_shared') else 'own'})"
                            ),
                        )
                        for device in devices
                    ],
                    multiple=True,
                )
            )

        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema), errors=errors
        )

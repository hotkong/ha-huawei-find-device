"""Config flow: paste a Huawei Cloud cookie, then select devices."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    BooleanSelectorConfig,
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
from .browser_login import BrowserLoginError, async_browser_login
from .const import (
    CONF_AMAP_KEY,
    CONF_AUTH_TIME,
    CONF_BROWSER_TOKEN,
    CONF_BROWSER_URL,
    CONF_HW_PASSWORD,
    CONF_HW_USERNAME,
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
BROWSER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BROWSER_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Optional(CONF_BROWSER_TOKEN, default=""): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_HW_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Required(CONF_HW_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional("store_credentials", default=True): BooleanSelector(
            BooleanSelectorConfig()
        ),
    }
)
PREVIEW_SCHEMA = vol.Schema({vol.Required(CONF_COOKIE): COOKIE_SELECTOR})


class HuaweiFindConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Huawei Cloud Find Device entry or re-authorize it."""

    VERSION = 1

    def __init__(self) -> None:
        self._cookie = ""
        self._devices: list[dict[str, Any]] = []
        self._user_id = ""
        self._browser_input: dict[str, Any] = {}
        self._login_task: Any = None
        self._login_cookie: str | None = None
        self._login_error: str | None = None
        self._preview_step = "cookie_preview"
        self._reauth_use_browser = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HuaweiFindOptionsFlow:
        return HuaweiFindOptionsFlow()

    # -- helpers ------------------------------------------------------------
    async def _validate_cookie(
        self, cookie: str
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        try:
            client = HuaweiFindClient(cookie)
        except HuaweiFindError:
            return None, "invalid_auth"
        try:
            devices = await client.async_get_devices()
        except HuaweiFindAuthError:
            return None, "invalid_auth"
        except HuaweiFindError as err:
            _LOGGER.warning("Cannot connect to Huawei Cloud: %s", err)
            return None, "cannot_connect"
        finally:
            self._user_id = client.user_id
            await client.async_close()
        if not devices:
            return None, "no_devices"
        self._cookie = cookie
        self._devices = devices
        return devices, None

    def _browser_options(self) -> dict[str, Any]:
        if not self._browser_input.get("store_credentials", True):
            return {}
        return {
            CONF_BROWSER_URL: self._browser_input.get(CONF_BROWSER_URL, ""),
            CONF_BROWSER_TOKEN: self._browser_input.get(CONF_BROWSER_TOKEN, ""),
            CONF_HW_USERNAME: self._browser_input.get(CONF_HW_USERNAME, ""),
            CONF_HW_PASSWORD: self._browser_input.get(CONF_HW_PASSWORD, ""),
        }

    def _browser_schema(self) -> vol.Schema:
        d = self._browser_input
        return vol.Schema(
            {
                vol.Required(
                    CONF_BROWSER_URL, default=d.get(CONF_BROWSER_URL, "")
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(
                    CONF_BROWSER_TOKEN, default=d.get(CONF_BROWSER_TOKEN, "")
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required(
                    CONF_HW_USERNAME, default=d.get(CONF_HW_USERNAME, "")
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Required(
                    CONF_HW_PASSWORD, default=d.get(CONF_HW_PASSWORD, "")
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(
                    "store_credentials", default=d.get("store_credentials", True)
                ): BooleanSelector(BooleanSelectorConfig()),
            }
        )

    async def _async_browser_login(self) -> None:
        try:
            self._login_cookie = await async_browser_login(
                self._browser_input[CONF_BROWSER_URL],
                self._browser_input[CONF_HW_USERNAME],
                self._browser_input[CONF_HW_PASSWORD],
                self._browser_input.get(CONF_BROWSER_TOKEN, ""),
            )
            self._login_error = None
        except BrowserLoginError as err:
            self._login_cookie = None
            self._login_error = str(err)
        except Exception as err:  # noqa: BLE001 - show any browser failure in the form
            self._login_cookie = None
            self._login_error = f"unexpected error: {err}"

    async def _async_browser_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"error": ""}

        if user_input is not None:
            self._reauth_use_browser = bool(user_input.get("browser_login"))
            self._browser_input = {
                CONF_BROWSER_URL: (user_input.get(CONF_BROWSER_URL) or "").strip(),
                CONF_BROWSER_TOKEN: (
                    user_input.get(CONF_BROWSER_TOKEN) or ""
                ).strip(),
                CONF_HW_USERNAME: (user_input.get(CONF_HW_USERNAME) or "").strip(),
                CONF_HW_PASSWORD: user_input.get(CONF_HW_PASSWORD) or "",
                "store_credentials": bool(
                    user_input.get("store_credentials", True)
                ),
            }
            if not (
                self._browser_input[CONF_BROWSER_URL]
                and self._browser_input[CONF_HW_USERNAME]
                and self._browser_input[CONF_HW_PASSWORD]
            ):
                errors["base"] = "browser_login_invalid"
            else:
                self._login_task = self.hass.async_create_task(
                    self._async_browser_login()
                )
                return self.async_show_progress(
                    step_id=step_id,
                    progress_action="browser_login",
                    progress_task=self._login_task,
                )

        if self._login_task is not None:
            if not self._login_task.done():
                return self.async_show_progress(
                    step_id=step_id,
                    progress_action="browser_login",
                    progress_task=self._login_task,
                )
            self._login_task = None
            if self._login_error:
                errors["base"] = "browser_login_failed"
                placeholders["error"] = self._login_error[:250]
                self._login_error = None
            elif self._login_cookie:
                self._cookie = self._login_cookie
                return self.async_show_progress_done(
                    next_step_id=self._preview_step
                )
            else:
                errors["base"] = "browser_login_failed"
                placeholders["error"] = "no cookie returned"

        return self.async_show_form(
            step_id=step_id,
            data_schema=self._browser_schema(),
            errors=errors,
            description_placeholders=placeholders,
        )

    # -- initial setup ------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="user", menu_options=["paste", "browser"]
        )

    async def async_step_paste(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cookie = (user_input.get(CONF_COOKIE) or "").strip()
            _, error = await self._validate_cookie(cookie)
            if error:
                errors["base"] = error
            else:
                if self._user_id:
                    await self.async_set_unique_id(f"huawei_{self._user_id}")
                    self._abort_if_unique_id_configured()
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="paste", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_browser(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._preview_step = "cookie_preview"
        return await self._async_browser_step("browser", user_input)

    async def async_step_cookie_preview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cookie = (user_input.get(CONF_COOKIE) or "").strip()
            _, error = await self._validate_cookie(cookie)
            if error:
                errors["base"] = error
            else:
                if self._user_id:
                    await self.async_set_unique_id(f"huawei_{self._user_id}")
                    self._abort_if_unique_id_configured()
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="cookie_preview",
            data_schema=vol.Schema(
                {vol.Required(CONF_COOKIE, default=self._cookie): COOKIE_SELECTOR}
            ),
            errors=errors,
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
                    ),
                    **self._browser_options(),
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
        self._preview_step = "reauth_preview"
        return self.async_show_menu(
            step_id="reauth_confirm",
            menu_options=["reauth_paste", "reauth_browser"],
        )

    def _finish_reauth(self) -> ConfigFlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reauth_failed")
        self.hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_COOKIE: self._cookie,
                CONF_AUTH_TIME: dt_util.utcnow().isoformat(),
            },
            options={**entry.options, **self._browser_options()},
        )
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(entry.entry_id)
        )
        return self.async_abort(reason="reauth_successful")

    def _reauth_schema(self) -> vol.Schema:
        d = self._browser_input
        return vol.Schema(
            {
                vol.Optional(
                    "browser_login", default=self._reauth_use_browser
                ): BooleanSelector(
                    BooleanSelectorConfig()
                ),
                vol.Optional(CONF_COOKIE, default=""): COOKIE_SELECTOR,
                vol.Optional(
                    CONF_BROWSER_URL, default=d.get(CONF_BROWSER_URL, "")
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(
                    CONF_BROWSER_TOKEN, default=d.get(CONF_BROWSER_TOKEN, "")
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(
                    CONF_HW_USERNAME, default=d.get(CONF_HW_USERNAME, "")
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(
                    CONF_HW_PASSWORD, default=d.get(CONF_HW_PASSWORD, "")
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(
                    "store_credentials", default=d.get("store_credentials", True)
                ): BooleanSelector(BooleanSelectorConfig()),
            }
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One page: paste a Cookie, or tick the browser-login option."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if not self._browser_input and entry is not None:
            self._browser_input = {
                CONF_BROWSER_URL: entry.options.get(CONF_BROWSER_URL, ""),
                CONF_BROWSER_TOKEN: entry.options.get(CONF_BROWSER_TOKEN, ""),
                CONF_HW_USERNAME: entry.options.get(CONF_HW_USERNAME, ""),
                CONF_HW_PASSWORD: entry.options.get(CONF_HW_PASSWORD, ""),
                "store_credentials": True,
            }
        self._preview_step = "reauth_preview"
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"error": ""}

        if user_input is not None:
            self._browser_input = {
                CONF_BROWSER_URL: (user_input.get(CONF_BROWSER_URL) or "").strip(),
                CONF_BROWSER_TOKEN: (
                    user_input.get(CONF_BROWSER_TOKEN) or ""
                ).strip(),
                CONF_HW_USERNAME: (user_input.get(CONF_HW_USERNAME) or "").strip(),
                CONF_HW_PASSWORD: user_input.get(CONF_HW_PASSWORD) or "",
                "store_credentials": bool(
                    user_input.get("store_credentials", True)
                ),
            }
            if not user_input.get("browser_login"):
                cookie = (user_input.get(CONF_COOKIE) or "").strip()
                _, error = await self._validate_cookie(cookie)
                if error:
                    errors["base"] = error
                else:
                    return self._finish_reauth()
            elif not (
                self._browser_input[CONF_BROWSER_URL]
                and self._browser_input[CONF_HW_USERNAME]
                and self._browser_input[CONF_HW_PASSWORD]
            ):
                errors["base"] = "browser_login_invalid"
            else:
                self._login_task = self.hass.async_create_task(
                    self._async_browser_login()
                )
                return self.async_show_progress(
                    step_id="reauth_confirm",
                    progress_action="browser_login",
                    progress_task=self._login_task,
                )

        if self._login_task is not None:
            if not self._login_task.done():
                return self.async_show_progress(
                    step_id="reauth_confirm",
                    progress_action="browser_login",
                    progress_task=self._login_task,
                )
            self._login_task = None
            if self._login_error:
                errors["base"] = "browser_login_failed"
                placeholders["error"] = self._login_error[:250]
                self._login_error = None
            elif self._login_cookie:
                self._cookie = self._login_cookie
                return self.async_show_progress_done(
                    next_step_id="reauth_preview"
                )
            else:
                errors["base"] = "browser_login_failed"
                placeholders["error"] = "no cookie returned"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._reauth_schema(),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reauth_paste(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cookie = (user_input.get(CONF_COOKIE) or "").strip()
            _, error = await self._validate_cookie(cookie)
            if error:
                errors["base"] = error
            else:
                return self._finish_reauth()

        return self.async_show_form(
            step_id="reauth_paste", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth_browser(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._preview_step = "reauth_preview"
        return await self._async_browser_step("reauth_browser", user_input)

    async def async_step_reauth_preview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cookie = (user_input.get(CONF_COOKIE) or "").strip()
            _, error = await self._validate_cookie(cookie)
            if error:
                errors["base"] = error
            else:
                return self._finish_reauth()

        return self.async_show_form(
            step_id="reauth_preview",
            data_schema=vol.Schema(
                {vol.Required(CONF_COOKIE, default=self._cookie): COOKIE_SELECTOR}
            ),
            errors=errors,
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
                auto = user_input.get("auto_login")
                if auto is None:
                    # Section omitted (collapsed/untouched): keep existing values
                    auto = entry.options
                options = {
                    CONF_GEOCODE_PROVIDER: user_input.get(
                        CONF_GEOCODE_PROVIDER, GEOCODE_AUTO
                    ),
                    CONF_AMAP_KEY: (user_input.get(CONF_AMAP_KEY) or "").strip(),
                    CONF_SCAN_INTERVAL: _scan_interval_value(
                        user_input.get(CONF_SCAN_INTERVAL)
                    ),
                    CONF_BROWSER_URL: (auto.get(CONF_BROWSER_URL) or "").strip(),
                    CONF_BROWSER_TOKEN: (auto.get(CONF_BROWSER_TOKEN) or "").strip(),
                    CONF_HW_USERNAME: (auto.get(CONF_HW_USERNAME) or "").strip(),
                    CONF_HW_PASSWORD: (auto.get(CONF_HW_PASSWORD) or "").strip(),
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
            vol.Optional("auto_login"): section(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_BROWSER_URL, default=current.get(CONF_BROWSER_URL, "")
                        ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                        vol.Optional(
                            CONF_BROWSER_TOKEN,
                            default=current.get(CONF_BROWSER_TOKEN, ""),
                        ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                        vol.Optional(
                            CONF_HW_USERNAME, default=current.get(CONF_HW_USERNAME, "")
                        ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                        vol.Optional(
                            CONF_HW_PASSWORD, default=current.get(CONF_HW_PASSWORD, "")
                        ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                    }
                ),
                {
                    "collapsed": not any(
                        current.get(key)
                        for key in (
                            CONF_BROWSER_URL,
                            CONF_HW_USERNAME,
                        )
                    )
                },
            ),
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

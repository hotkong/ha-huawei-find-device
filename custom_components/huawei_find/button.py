"""Button entities: refresh all devices, or a single device."""

from __future__ import annotations

import asyncio

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import HuaweiFindRuntime, async_refresh_device
from .const import CONF_AUTH_TIME, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: HuaweiFindRuntime = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = [HuaweiRefreshAllButton(hass, runtime)]
    entities.extend(
        HuaweiDeviceRefreshButton(hass, runtime, device)
        for device in runtime.devices.values()
    )
    async_add_entities(entities)


class HuaweiRefreshAllButton(ButtonEntity):
    """Press = query the location of every device on the account."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:crosshairs-gps"
    _attr_translation_key = "refresh_all"

    def __init__(self, hass: HomeAssistant, runtime: HuaweiFindRuntime) -> None:
        self.hass = hass
        self._runtime = runtime
        self._attr_unique_id = f"{DOMAIN}_{runtime.entry.entry_id}_refresh_all"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"account_{self._runtime.entry.entry_id}")},
            name="Huawei Cloud Find Device",
            manufacturer="Huawei",
            model="Huawei Cloud Find Device",
        )

    @property
    def extra_state_attributes(self) -> dict:
        auth_time = self._runtime.entry.data.get(CONF_AUTH_TIME)
        days = None
        if auth_time:
            parsed = dt_util.parse_datetime(auth_time)
            if parsed is not None:
                days = round((dt_util.utcnow() - parsed).total_seconds() / 86400, 1)
        results = self._runtime.results
        return {
            "device_count": len(self._runtime.devices),
            "refreshing": sorted(self._runtime.refreshing),
            "last_status": {
                device["name"]: results.get(device_id, {}).get("status")
                for device_id, device in self._runtime.devices.items()
            },
            "auth_time": auth_time,
            "days_since_auth": days,
        }

    async def async_press(self) -> None:
        """Run in background so the frontend is not blocked."""
        self.hass.async_create_task(
            self._refresh_all(), f"huawei_find refresh all {self._runtime.entry.entry_id}"
        )

    async def _refresh_all(self) -> None:
        tasks = [
            async_refresh_device(self.hass, self._runtime, device_id)
            for device_id in list(self._runtime.devices)
        ]
        await asyncio.gather(*tasks)


class HuaweiDeviceRefreshButton(ButtonEntity):
    """Press = query the location of this single device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:crosshairs-gps"
    _attr_translation_key = "refresh"

    def __init__(
        self, hass: HomeAssistant, runtime: HuaweiFindRuntime, device: dict
    ) -> None:
        self.hass = hass
        self._runtime = runtime
        self._device = device
        self._attr_unique_id = f"{DOMAIN}_{runtime.entry.entry_id}_{device['id']}_refresh"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device["id"])},
            name=self._device.get("name") or "Huawei device",
            manufacturer="Huawei",
            model=self._device.get("model") or None,
        )

    @property
    def extra_state_attributes(self) -> dict:
        result = self._runtime.results.get(self._device["id"], {})
        last_attempt = result.get("last_attempt")
        return {
            "device_id": self._device["id"],
            "is_shared": self._device.get("is_shared", False),
            "refreshing": self._device["id"] in self._runtime.refreshing,
            "status": result.get("status") or "pending",
            "stale": result.get("stale", False),
            "last_attempt": (
                last_attempt.isoformat()
                if hasattr(last_attempt, "isoformat")
                else last_attempt
            ),
        }

    async def async_press(self) -> None:
        self.hass.async_create_task(
            async_refresh_device(self.hass, self._runtime, self._device["id"]),
            f"huawei_find refresh {self._device['id']}",
        )

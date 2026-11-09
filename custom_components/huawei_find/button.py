"""Button entities: refresh all devices, or a single device."""

from __future__ import annotations

import asyncio

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import (
    HuaweiFindRuntime,
    async_refresh_device,
    async_ring_device,
    device_identifiers,
    enabled_device_ids,
    signal_account,
    supports_ring,
)
from .const import CONF_AUTH_TIME, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: HuaweiFindRuntime = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = [HuaweiRefreshAllButton(hass, runtime)]
    for device in runtime.devices.values():
        entities.append(HuaweiDeviceRefreshButton(hass, runtime, device))
        if supports_ring(device):
            entities.append(HuaweiRingButton(hass, runtime, device))
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
        last_heartbeat = self._runtime.last_heartbeat
        return {
            "device_count": len(self._runtime.devices),
            "active_device_count": len(enabled_device_ids(self.hass, self._runtime)),
            "refreshing": sorted(self._runtime.refreshing),
            "last_status": {
                device["name"]: results.get(device_id, {}).get("status")
                for device_id, device in self._runtime.devices.items()
            },
            "auth_time": auth_time,
            "days_since_auth": days,
            "heartbeat_ok": self._runtime.heartbeat_ok,
            "heartbeat_failures": self._runtime.heartbeat_failures,
            "last_heartbeat": (
                last_heartbeat.isoformat()
                if hasattr(last_heartbeat, "isoformat")
                else last_heartbeat
            ),
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_account(self._runtime.entry.entry_id),
                self._handle_account_update,
            )
        )

    @callback
    def _handle_account_update(self) -> None:
        self.async_write_ha_state()

    async def async_press(self) -> None:
        """Run in background so the frontend is not blocked."""
        self.hass.async_create_task(
            self._refresh_all(), f"huawei_find refresh all {self._runtime.entry.entry_id}"
        )

    async def _refresh_all(self) -> None:
        tasks = [
            async_refresh_device(self.hass, self._runtime, device_id)
            for device_id in enabled_device_ids(self.hass, self._runtime)
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
            identifiers=device_identifiers(self._runtime.entry.entry_id, self._device["id"]),
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


class HuaweiRingButton(ButtonEntity):
    """Ask this device to play its alarm sound (find my phone)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:bell-ring"
    _attr_translation_key = "ring"

    def __init__(
        self, hass: HomeAssistant, runtime: HuaweiFindRuntime, device: dict
    ) -> None:
        self.hass = hass
        self._runtime = runtime
        self._device = device
        self._attr_unique_id = f"{DOMAIN}_{runtime.entry.entry_id}_{device['id']}_ring"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers=device_identifiers(self._runtime.entry.entry_id, self._device["id"]),
            name=self._device.get("name") or "Huawei device",
            manufacturer="Huawei",
            model=self._device.get("model") or None,
        )

    @property
    def extra_state_attributes(self) -> dict:
        last = self._runtime.ring_results.get(self._device["id"])
        return {
            "device_id": self._device["id"],
            "last_ring": last.isoformat() if hasattr(last, "isoformat") else last,
        }

    async def async_press(self) -> None:
        self.hass.async_create_task(
            self._ring(), f"huawei_find ring {self._device['id']}"
        )

    async def _ring(self) -> None:
        from homeassistant.components import persistent_notification

        result = await async_ring_device(self.hass, self._runtime, self._device["id"])
        notification_id = f"{DOMAIN}_ring_{self._device['id']}"
        if not result.get("ok"):
            persistent_notification.async_create(
                self.hass,
                f"{self._device.get('name')}: {result.get('error')}",
                title="Huawei Cloud Find Device: ring failed",
                notification_id=notification_id,
            )
        else:
            persistent_notification.async_dismiss(self.hass, notification_id)

"""Sensor: state shows the address, falling back to "lat, lon"."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HuaweiFindRuntime, signal_update
from .const import (
    ATTR_ACCURACY,
    ATTR_ADDRESS,
    ATTR_BATTERY,
    ATTR_CHARGING,
    ATTR_COORD_SYS,
    ATTR_DEVICE_ID,
    ATTR_DEVICE_TIME,
    ATTR_ERROR,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_SCREEN_LOCKED,
    ATTR_STATUS,
    DOMAIN,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: HuaweiFindRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        HuaweiLocationSensor(runtime, device) for device in runtime.devices.values()
    )


class HuaweiLocationSensor(SensorEntity):
    """State = address (or "lat, lon" when geocoding is disabled)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:map-marker"
    _attr_translation_key = "location"
    _attr_native_value: str | None = None

    def __init__(self, runtime: HuaweiFindRuntime, device: dict) -> None:
        self._runtime = runtime
        self._device = device
        self._attr_unique_id = f"{DOMAIN}_{runtime.entry.entry_id}_{device['id']}_location"
        cached = runtime.results.get(device["id"], {})
        self._attr_extra_state_attributes = self._build_attributes(cached)
        self._attr_native_value = self._format_state(cached)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device["id"])},
            name=self._device.get("name") or "Huawei device",
            manufacturer="Huawei",
            model=self._device.get("model") or None,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self._runtime.entry.entry_id, self._device["id"]),
                self._handle_update,
            )
        )

    def _format_state(self, result: dict) -> str | None:
        """Address first, coordinates as fallback."""
        address = result.get("address")
        if address:
            return str(address)[:250]
        latitude = result.get("latitude")
        longitude = result.get("longitude")
        if latitude is None or longitude is None:
            return None
        return f"{latitude:.6f}, {longitude:.6f}"

    @callback
    def _handle_update(self) -> None:
        result = self._runtime.results.get(self._device["id"], {})
        value = self._format_state(result)
        if value is not None:
            self._attr_native_value = value
        self._attr_extra_state_attributes = self._build_attributes(result)
        self.async_write_ha_state()

    def _build_attributes(self, result: dict) -> dict:
        attrs = {
            ATTR_DEVICE_ID: self._device["id"],
            ATTR_STATUS: result.get("status") or "pending",
            "is_shared": self._device.get("is_shared", False),
            "refreshing": self._device["id"] in self._runtime.refreshing,
        }
        if result.get("latitude") is not None:
            located_at = result.get("located_at")
            attrs.update(
                {
                    ATTR_LATITUDE: result.get("latitude"),
                    ATTR_LONGITUDE: result.get("longitude"),
                    ATTR_ACCURACY: result.get("accuracy"),
                    ATTR_BATTERY: result.get("battery"),
                    ATTR_CHARGING: result.get("battery_charging"),
                    ATTR_SCREEN_LOCKED: result.get("screen_locked"),
                    ATTR_COORD_SYS: result.get("coordinate_system"),
                    ATTR_DEVICE_TIME: result.get("device_time"),
                    "located_at": located_at.isoformat()
                    if isinstance(located_at, datetime)
                    else located_at,
                    "stale": result.get("stale", False),
                    "last_attempt": (
                        result["last_attempt"].isoformat()
                        if isinstance(result.get("last_attempt"), datetime)
                        else result.get("last_attempt")
                    ),
                }
            )
        if result.get("address"):
            attrs[ATTR_ADDRESS] = result["address"]
        if result.get("last_error"):
            attrs[ATTR_ERROR] = result["last_error"]
        return attrs

"""Sensor: state shows the address, falling back to "lat, lon"."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HuaweiFindRuntime, device_identifiers, signal_update
from .const import (
    GEOCODE_NONE,
    ATTR_ACCURACY,
    ATTR_ADDRESS,
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
    entities: list[SensorEntity] = []
    for device in runtime.devices.values():
        entities.append(HuaweiLocationSensor(runtime, device))
        entities.append(HuaweiBatterySensor(runtime, device))
        entities.append(HuaweiPositionStatusSensor(runtime, device))
    async_add_entities(entities)


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
            identifiers=device_identifiers(self._runtime.entry.entry_id, self._device["id"]),
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
        """Address first; coordinates only when no provider is configured."""
        address = result.get("address")
        if address:
            return str(address)[:250]

        latitude = result.get("latitude")
        longitude = result.get("longitude")
        if latitude is None or longitude is None:
            return None

        if self._runtime.geocode_provider != GEOCODE_NONE:
            return self._address_unavailable_text()
        return f"{latitude:.6f}, {longitude:.6f}"

    def _address_unavailable_text(self) -> str:
        hass = getattr(self, "hass", None)
        language = ""
        if hass is not None:
            language = (hass.config.language or "").lower()
        return "\u5730\u5740\u89e3\u6790\u5931\u8d25" if language.startswith("zh") else "Address unavailable"

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
                    ATTR_SCREEN_LOCKED: result.get("screen_locked"),
                    ATTR_COORD_SYS: result.get("coordinate_system"),
                    ATTR_DEVICE_TIME: result.get("device_time"),
                    "located_at": located_at.isoformat()
                    if isinstance(located_at, datetime)
                    else located_at,
                    "last_attempt": (
                        result["last_attempt"].isoformat()
                        if isinstance(result.get("last_attempt"), datetime)
                        else result.get("last_attempt")
                    ),
                }
            )
        if result.get("address"):
            attrs[ATTR_ADDRESS] = result["address"]
            attrs["address_stale"] = result.get("address_stale", False)
        if result.get("last_error"):
            attrs[ATTR_ERROR] = result["last_error"]
        return attrs


class HuaweiBatterySensor(SensorEntity):
    """Battery level reported by the Huawei Cloud locate result."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_value: int | None = None

    def __init__(self, runtime: HuaweiFindRuntime, device: dict) -> None:
        self._runtime = runtime
        self._device = device
        self._attr_unique_id = f"{DOMAIN}_{runtime.entry.entry_id}_{device['id']}_battery"
        self._attr_native_value = self._value_from(runtime.results.get(device["id"], {}))

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers=device_identifiers(self._runtime.entry.entry_id, self._device["id"]),
            name=self._device.get("name") or "Huawei device",
            manufacturer="Huawei",
            model=self._device.get("model") or None,
        )

    @staticmethod
    def _value_from(result: dict) -> int | None:
        value = result.get("battery")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self._runtime.entry.entry_id, self._device["id"]),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        result = self._runtime.results.get(self._device["id"], {})
        value = self._value_from(result)
        if value is not None:
            self._attr_native_value = value
        self.async_write_ha_state()


class HuaweiPositionStatusSensor(SensorEntity):
    """Status of the last locate attempt (ok / stale / unknown)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "position_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "stale", "unknown"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:map-marker-check"
    _attr_native_value: str | None = None

    def __init__(self, runtime: HuaweiFindRuntime, device: dict) -> None:
        self._runtime = runtime
        self._device = device
        self._attr_unique_id = (
            f"{DOMAIN}_{runtime.entry.entry_id}_{device['id']}_position_status"
        )
        self._attr_native_value = self._value_from(
            runtime.results.get(device["id"], {})
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers=device_identifiers(self._runtime.entry.entry_id, self._device["id"]),
            name=self._device.get("name") or "Huawei device",
            manufacturer="Huawei",
            model=self._device.get("model") or None,
        )

    @staticmethod
    def _value_from(result: dict) -> str:
        if not result:
            return "unknown"
        if result.get("status") != "ok" or result.get("stale"):
            return "stale"
        return "ok"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self._runtime.entry.entry_id, self._device["id"]),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self._attr_native_value = self._value_from(
            self._runtime.results.get(self._device["id"], {})
        )
        self.async_write_ha_state()

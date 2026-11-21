"""Binary sensors derived from the locate result (charging state)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HuaweiFindRuntime, build_device_info, signal_update
from .const import DOMAIN
from .helpers import supports_charging


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: HuaweiFindRuntime = hass.data[DOMAIN][entry.entry_id]
    registry = er.async_get(hass)
    entities: list[HuaweiChargingSensor] = []
    for device in runtime.devices.values():
        if supports_charging(device):
            entities.append(HuaweiChargingSensor(runtime, device))
            continue
        # Wearables never report charging: remove any entity left from older versions
        unique_id = f"{DOMAIN}_{entry.entry_id}_{device['id']}_charging"
        entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
        if entity_id:
            registry.async_remove(entity_id)
    async_add_entities(entities)


class HuaweiChargingSensor(BinarySensorEntity):
    """Whether the device reported being on a charger."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_is_on: bool | None = None

    def __init__(self, runtime: HuaweiFindRuntime, device: dict) -> None:
        self._runtime = runtime
        self._device = device
        self._attr_unique_id = (
            f"{DOMAIN}_{runtime.entry.entry_id}_{device['id']}_charging"
        )
        self._attr_is_on = self._state_from(runtime.results.get(device["id"], {}))

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self._runtime.entry.entry_id, self._device)

    @staticmethod
    def _state_from(result: dict) -> bool | None:
        value = result.get("battery_charging")
        if value is None:
            return None
        return bool(value)

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
        value = self._state_from(result)
        if value is not None:
            self._attr_is_on = value
        self.async_write_ha_state()

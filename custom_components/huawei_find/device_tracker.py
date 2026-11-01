"""Device tracker built from the coordinates returned by Huawei Cloud."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HuaweiFindRuntime, signal_update
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: HuaweiFindRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        HuaweiLocationTracker(runtime, device) for device in runtime.devices.values()
    )


class HuaweiLocationTracker(TrackerEntity):
    """One GPS tracker per device; HA derives home/not_home from zones."""

    _attr_has_entity_name = True
    _attr_name = None  # use the device name, e.g. device_tracker.huawei_mate_40_pro
    _attr_source_type = SourceType.GPS
    _attr_entity_category = None  # trackers are diagnostic by default; keep them visible
    _attr_latitude: float | None = None
    _attr_longitude: float | None = None
    _attr_location_accuracy: float = 0

    def __init__(self, runtime: HuaweiFindRuntime, device: dict) -> None:
        self._runtime = runtime
        self._device = device
        self._attr_unique_id = f"{DOMAIN}_{runtime.entry.entry_id}_{device['id']}_tracker"
        self._apply_result(runtime.results.get(device["id"], {}))

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

    def _apply_result(self, result: dict) -> None:
        if result.get("latitude") is not None:
            self._attr_latitude = result["latitude"]
            self._attr_longitude = result["longitude"]
            accuracy = result.get("accuracy")
            self._attr_location_accuracy = float(accuracy) if accuracy else 0.0

        located_at = result.get("located_at")
        attrs = {
            "device_id": self._device["id"],
            "is_shared": self._device.get("is_shared", False),
            "status": result.get("status") or "pending",
            "refreshing": self._device["id"] in self._runtime.refreshing,
        }
        if result.get("latitude") is not None:
            attrs["coordinate_system"] = result.get("coordinate_system")
            attrs["located_at"] = (
                located_at.isoformat() if isinstance(located_at, datetime) else located_at
            )
            last_attempt = result.get("last_attempt")
            attrs["last_attempt"] = (
                last_attempt.isoformat()
                if isinstance(last_attempt, datetime)
                else last_attempt
            )
        if result.get("address"):
            attrs["address"] = result["address"]
        if result.get("last_error"):
            attrs["last_error"] = result["last_error"]
        self._attr_extra_state_attributes = attrs

    @callback
    def _handle_update(self) -> None:
        self._apply_result(self._runtime.results.get(self._device["id"], {}))
        self.async_write_ha_state()

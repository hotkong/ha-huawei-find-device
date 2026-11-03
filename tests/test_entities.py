"""Smoke tests: entity classes must construct without a running HA."""

import unittest
from types import SimpleNamespace

from custom_components.huawei_find import HuaweiFindRuntime
from custom_components.huawei_find.binary_sensor import HuaweiChargingSensor
from custom_components.huawei_find.sensor import (
    HuaweiBatterySensor,
    HuaweiLocationSensor,
    HuaweiPositionStatusSensor,
)

DEVICE = {
    "id": "abc123",
    "name": "Test Phone",
    "model": "HUAWEI Test",
    "is_shared": False,
}


class FakeRuntime(HuaweiFindRuntime):
    """Minimal object exposing what the entity constructors touch."""

    def __init__(self, results=None, geocode_provider="none"):
        self.entry = SimpleNamespace(entry_id="01TEST", data={}, options={})
        self.results = results or {}
        self.refreshing = set()
        self.devices = {DEVICE["id"]: DEVICE}
        self.geocode_provider = geocode_provider


RESULT = {
    "status": "ok",
    "latitude": 31.42,
    "longitude": 120.44,
    "accuracy": 12.0,
    "battery": 76,
    "battery_charging": False,
    "stale": False,
    "address": "Somewhere",
    "located_at": None,
    "last_attempt": None,
}


class EntityConstructionTests(unittest.TestCase):
    def test_location_sensor(self):
        entity = HuaweiLocationSensor(FakeRuntime({DEVICE["id"]: RESULT}), DEVICE)
        self.assertEqual(entity.native_value, "Somewhere")
        self.assertEqual(entity.extra_state_attributes["latitude"], 31.42)

    def test_location_sensor_without_address(self):
        result = dict(RESULT)
        result.pop("address")
        entity = HuaweiLocationSensor(FakeRuntime({DEVICE["id"]: result}), DEVICE)
        self.assertIn("31.420000", entity.native_value)

    def test_location_sensor_hides_coords_when_provider_configured(self):
        result = dict(RESULT)
        result.pop("address")
        entity = HuaweiLocationSensor(
            FakeRuntime({DEVICE["id"]: result}, "huawei"), DEVICE
        )
        self.assertEqual(entity.native_value, "Address unavailable")
        self.assertNotIn("31.42", entity.native_value)
        # coordinates stay available as attributes
        self.assertEqual(entity.extra_state_attributes["latitude"], 31.42)

    def test_location_sensor_keeps_address_with_provider(self):
        entity = HuaweiLocationSensor(
            FakeRuntime({DEVICE["id"]: RESULT}, "huawei"), DEVICE
        )
        self.assertEqual(entity.native_value, "Somewhere")

    def test_battery_sensor(self):
        entity = HuaweiBatterySensor(FakeRuntime({DEVICE["id"]: RESULT}), DEVICE)
        self.assertEqual(entity.native_value, 76)
        self.assertTrue(entity.unique_id.endswith("_battery"))

    def test_charging_sensor(self):
        entity = HuaweiChargingSensor(FakeRuntime({DEVICE["id"]: RESULT}), DEVICE)
        self.assertIs(entity.is_on, False)

    def test_position_status_ok(self):
        entity = HuaweiPositionStatusSensor(FakeRuntime({DEVICE["id"]: RESULT}), DEVICE)
        self.assertEqual(entity.native_value, "ok")

    def test_position_status_stale(self):
        result = dict(RESULT, stale=True)
        entity = HuaweiPositionStatusSensor(FakeRuntime({DEVICE["id"]: result}), DEVICE)
        self.assertEqual(entity.native_value, "stale")

    def test_position_status_unknown(self):
        entity = HuaweiPositionStatusSensor(FakeRuntime({}), DEVICE)
        self.assertEqual(entity.native_value, "unknown")

    def test_position_status_failed_without_coords(self):
        result = {"status": "error", "last_error": "boom", "stale": False}
        entity = HuaweiPositionStatusSensor(FakeRuntime({DEVICE["id"]: result}), DEVICE)
        self.assertEqual(entity.native_value, "stale")

    def test_empty_results(self):
        runtime = FakeRuntime({})
        for cls in (
            HuaweiLocationSensor,
            HuaweiBatterySensor,
            HuaweiChargingSensor,
            HuaweiPositionStatusSensor,
        ):
            entity = cls(runtime, DEVICE)
            self.assertIsNotNone(entity.unique_id)


if __name__ == "__main__":
    unittest.main()

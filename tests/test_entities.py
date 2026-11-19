"""Smoke tests: entity classes must construct without a running HA."""

import unittest
from types import SimpleNamespace

from custom_components.huawei_find import HuaweiFindRuntime
from custom_components.huawei_find.binary_sensor import HuaweiChargingSensor
from custom_components.huawei_find.helpers import battery_components
from custom_components.huawei_find.sensor import (
    HuaweiBatterySensor,
    HuaweiComponentBatterySensor,
    HuaweiLocationSensor,
    HuaweiPositionStatusSensor,
)

DEVICE = {
    "id": "abc123",
    "name": "Test Phone",
    "model": "HUAWEI Test",
    "is_shared": False,
}

EARBUDS = {
    "id": "abc123",
    "name": "Test Buds",
    "model": "HUAWEI FreeBuds",
    "per_device_type": "1",
    "components": ["case", "left", "right"],
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


EARBUD_RESULT = {
    "status": "ok",
    "latitude": 31.42,
    "longitude": 120.44,
    "battery": 57,
    "battery_stale": True,
    "batteries": {
        "case": {"percentage": None, "stale": True, "reported_at": None},
        "left": {
            "percentage": 57,
            "stale": True,
            "reported_at": "2026-11-09T11:49:19+00:00",
            "connect_type": -1,
        },
        "right": {
            "percentage": 63,
            "stale": True,
            "reported_at": "2026-11-09T11:49:19+00:00",
            "connect_type": -1,
        },
    },
}


class EarbudEntityTests(unittest.TestCase):
    def _runtime(self):
        return FakeRuntime({EARBUDS["id"]: EARBUD_RESULT})

    def test_each_component_has_its_own_sensor(self):
        runtime = self._runtime()
        left = HuaweiComponentBatterySensor(runtime, EARBUDS, "left")
        right = HuaweiComponentBatterySensor(runtime, EARBUDS, "right")
        case = HuaweiComponentBatterySensor(runtime, EARBUDS, "case")
        self.assertEqual(left.native_value, 57)
        self.assertEqual(right.native_value, 63)
        self.assertIsNone(case.native_value)
        self.assertEqual(left.translation_key, "battery_left")

    def test_component_attributes_expose_freshness(self):
        entity = HuaweiComponentBatterySensor(self._runtime(), EARBUDS, "left")
        self.assertEqual(
            entity.extra_state_attributes["reported_at"],
            "2026-11-09T11:49:19+00:00",
        )
        self.assertTrue(entity.extra_state_attributes["stale"])

    def test_aggregate_entity_is_lowest_and_keeps_legacy_unique_id(self):
        entity = HuaweiBatterySensor(self._runtime(), EARBUDS)
        self.assertEqual(entity.native_value, 57)
        self.assertEqual(entity.translation_key, "battery_lowest")
        self.assertTrue(entity.unique_id.endswith("_battery"))
        attributes = entity.extra_state_attributes
        self.assertEqual(attributes["aggregate"], "min")
        self.assertEqual(attributes["components"], {"case": None, "left": 57, "right": 63})
        self.assertTrue(attributes["stale"])

    def test_phone_battery_keeps_plain_translation_key(self):
        entity = HuaweiBatterySensor(FakeRuntime({DEVICE["id"]: RESULT}), DEVICE)
        self.assertEqual(entity.translation_key, "battery")


class BatteryComponentSelectionTests(unittest.TestCase):
    def test_earbuds_report_component_entities(self):
        self.assertEqual(battery_components(EARBUDS), ("case", "left", "right"))

    def test_earbuds_without_cached_components_fall_back_to_buds(self):
        device = dict(EARBUDS)
        device["components"] = []
        self.assertEqual(battery_components(device), ("left", "right"))

    def test_phones_and_watches_have_no_component_sensors(self):
        self.assertEqual(battery_components(DEVICE), ())
        self.assertEqual(battery_components({"per_device_type": "2"}), ())


if __name__ == "__main__":
    unittest.main()

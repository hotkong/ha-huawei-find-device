"""enabled_device_ids registry filtering tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.huawei_find.coordinator import enabled_device_ids


class FakeDeviceRegistry:
    def __init__(self, devices):
        self._devices = devices

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        return self._devices.get(identifier[1].split(":", 1)[1])


class FakeEntityRegistry:
    def __init__(self, entries):
        self._entries = entries

    def async_entries_for_device(self, registry, device_id, include_disabled_entities=False):
        return self._entries.get(device_id, [])


def _device(device_id, disabled_by=None):
    return SimpleNamespace(id=device_id, disabled_by=disabled_by)


def _entity(disabled_by=None):
    return SimpleNamespace(disabled_by=disabled_by)


class EnabledDeviceIdsTests(unittest.TestCase):
    def _run(self, devices, entities):
        runtime = SimpleNamespace(
            entry=SimpleNamespace(entry_id="e1"),
            devices={device_id: {} for device_id in devices},
        )
        with patch(
            "custom_components.huawei_find.coordinator.dr.async_get",
            return_value=FakeDeviceRegistry(devices),
        ), patch(
            "custom_components.huawei_find.coordinator.er.async_get",
            return_value=FakeEntityRegistry(entities),
        ), patch(
            "custom_components.huawei_find.coordinator.er.async_entries_for_device",
            side_effect=lambda registry, device_id, include_disabled_entities=False: entities.get(
                device_id, []
            ),
        ):
            return enabled_device_ids(SimpleNamespace(), runtime)

    def test_missing_device_counts_as_active(self):
        result = self._run({"d1": None}, {})
        self.assertEqual(result, ["d1"])

    def test_disabled_device_is_skipped(self):
        result = self._run({"d2": _device("dev2", disabled_by="user")}, {})
        self.assertEqual(result, [])

    def test_device_with_all_entities_disabled_is_skipped(self):
        devices = {"d3": _device("dev3")}
        entities = {"dev3": [_entity("user"), _entity("integration")]}
        self.assertEqual(self._run(devices, entities), [])

    def test_active_device_is_kept(self):
        devices = {"d4": _device("dev4")}
        entities = {"dev4": [_entity("user"), _entity()]}
        self.assertEqual(self._run(devices, entities), ["d4"])

    def test_mixed_selection(self):
        devices = {
            "a": _device("deva"),
            "b": _device("devb", disabled_by="user"),
            "c": None,
        }
        entities = {"deva": [_entity()]}
        self.assertEqual(sorted(self._run(devices, entities)), ["a", "c"])


if __name__ == "__main__":
    unittest.main()

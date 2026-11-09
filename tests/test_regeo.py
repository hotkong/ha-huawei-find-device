"""Re-resolving addresses after a provider change."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.huawei_find.coordinator import _async_refresh_addresses


def _runtime(results, provider="auto", key=""):
    return SimpleNamespace(
        results=results,
        geocode_provider=provider,
        amap_key=key,
        unloading=False,
        entry=SimpleNamespace(entry_id="entry"),
        store=None,
    )


class RegeocodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_provider_clears_addresses(self):
        results = {
            "d1": {"latitude": 1.0, "longitude": 2.0, "address": "A", "address_stale": True}
        }
        with patch("custom_components.huawei_find.coordinator.async_dispatcher_send"), patch(
            "custom_components.huawei_find.coordinator._async_save_results", AsyncMock()
        ):
            await _async_refresh_addresses(SimpleNamespace(), _runtime(results, "none"))
        self.assertNotIn("address", results["d1"])
        self.assertNotIn("address_stale", results["d1"])

    async def test_new_address_is_applied(self):
        results = {"d1": {"latitude": 1.0, "longitude": 2.0, "address": "OLD"}}
        with patch(
            "custom_components.huawei_find.coordinator.async_reverse_geocode",
            AsyncMock(return_value="NEW"),
        ), patch("custom_components.huawei_find.coordinator.async_dispatcher_send"), patch(
            "custom_components.huawei_find.coordinator._async_save_results", AsyncMock()
        ):
            await _async_refresh_addresses(SimpleNamespace(), _runtime(results))
        self.assertEqual(results["d1"]["address"], "NEW")
        self.assertFalse(results["d1"]["address_stale"])

    async def test_failed_lookup_keeps_old_address_but_marks_stale(self):
        results = {"d1": {"latitude": 1.0, "longitude": 2.0, "address": "OLD"}}
        with patch(
            "custom_components.huawei_find.coordinator.async_reverse_geocode",
            AsyncMock(return_value=None),
        ), patch("custom_components.huawei_find.coordinator.async_dispatcher_send"), patch(
            "custom_components.huawei_find.coordinator._async_save_results", AsyncMock()
        ):
            await _async_refresh_addresses(SimpleNamespace(), _runtime(results))
        self.assertEqual(results["d1"]["address"], "OLD")
        self.assertTrue(results["d1"]["address_stale"])

    async def test_skips_entries_without_coordinates(self):
        results = {"d1": {"status": "error", "last_error": "boom"}}
        geocode = AsyncMock(return_value="X")
        with patch("custom_components.huawei_find.coordinator.async_reverse_geocode", geocode), \
             patch("custom_components.huawei_find.coordinator.async_dispatcher_send"), \
             patch("custom_components.huawei_find.coordinator._async_save_results", AsyncMock()):
            await _async_refresh_addresses(SimpleNamespace(), _runtime(results))
        geocode.assert_not_called()


if __name__ == "__main__":
    unittest.main()

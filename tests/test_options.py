"""Hot-applying options without reloading the entry."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.huawei_find.options import apply_options
from custom_components.huawei_find.const import (
    CONF_AMAP_KEY,
    CONF_GEOCODE_PROVIDER,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)


def _hass(runtime):
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    hass.async_create_task = MagicMock(return_value=MagicMock())
    return hass


class ApplyOptionsTests(unittest.TestCase):
    def test_applies_provider_key_and_interval(self):
        runtime = SimpleNamespace(
            geocode_provider="none",
            amap_key="",
            auto_refresh_unsub=None,
            tasks=set(),
        )
        entry = SimpleNamespace(entry_id="entry")
        with patch(
            "custom_components.huawei_find.coordinator.async_track_time_interval",
            return_value=MagicMock(),
        ) as scheduler:
            applied = apply_options(
                _hass(runtime),
                entry,
                {
                    CONF_GEOCODE_PROVIDER: "osm",
                    CONF_AMAP_KEY: "secret",
                    CONF_SCAN_INTERVAL: 30,
                },
            )
        self.assertTrue(applied)
        self.assertEqual(runtime.geocode_provider, "osm")
        self.assertEqual(runtime.amap_key, "secret")
        scheduler.assert_called_once()

    def test_zero_interval_cancels_existing_timer(self):
        unsub = MagicMock()
        runtime = SimpleNamespace(
            geocode_provider="auto",
            amap_key="",
            auto_refresh_unsub=unsub,
            tasks=set(),
        )
        with patch(
            "custom_components.huawei_find.coordinator.async_track_time_interval"
        ) as scheduler:
            apply_options(
                _hass(runtime),
                SimpleNamespace(entry_id="entry"),
                {CONF_SCAN_INTERVAL: 0},
            )
        unsub.assert_called_once()
        scheduler.assert_not_called()
        self.assertIsNone(runtime.auto_refresh_unsub)

    def test_missing_runtime_returns_false(self):
        hass = SimpleNamespace(data={DOMAIN: {}})
        applied = apply_options(hass, SimpleNamespace(entry_id="x"), {})
        self.assertFalse(applied)


if __name__ == "__main__":
    unittest.main()

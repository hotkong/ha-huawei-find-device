"""Unit tests for the Huawei Cloud Find Device integration (stdlib only)."""

import json
import unittest

from custom_components.huawei_find import _merge_result
from custom_components.huawei_find.api import (
    _parse_locate_info,
    bd09_to_gcj02,
    gcj02_to_wgs84,
    parse_cookie,
)


class CoordinateTests(unittest.TestCase):
    def test_gcj02_to_wgs84_known_pair(self):
        # GCJ-02 of the sample point -> the WGS-84 value Huawei reports.
        lng, lat = gcj02_to_wgs84(120.4506728, 31.4185801)
        self.assertAlmostEqual(lat, 31.4204, places=4)
        self.assertAlmostEqual(lng, 120.4461, places=4)

    def test_out_of_china_is_untouched(self):
        self.assertEqual(gcj02_to_wgs84(2.3522, 48.8566), (2.3522, 48.8566))

    def test_bd09_to_gcj02_roundtrip_sanity(self):
        lng, lat = bd09_to_gcj02(120.45, 31.42)
        self.assertTrue(119 < lng < 121)
        self.assertTrue(30 < lat < 32)


class CookieTests(unittest.TestCase):
    def test_parse_cookie(self):
        cookies = parse_cookie("CSRFToken=abc; USER_ID=123; empty=")
        self.assertEqual(cookies["CSRFToken"], "abc")
        self.assertEqual(cookies["USER_ID"], "123")
        self.assertEqual(cookies["empty"], "")

    def test_parse_cookie_newlines(self):
        cookies = parse_cookie("A=1\nB=2")
        self.assertEqual(cookies, {"A": "1", "B": "2"})


class LocateInfoTests(unittest.TestCase):
    def test_prefers_wgs84_fields(self):
        payload = {
            "locateInfo": json.dumps(
                {
                    "accuracy": 13.4,
                    "latitude": 31.424588,
                    "longitude": 120.457152,
                    "latitude_WGS": 31.4204037,
                    "longitude_WGS": 120.4460565,
                    "batteryStatus": json.dumps(
                        {"isCharging": "0", "percentage": "12"}
                    ),
                    "networkInfo": json.dumps(
                        {"name": "tytcp_5G", "signal": "4", "type": "0"}
                    ),
                    "isLockScreen": 1,
                }
            )
        }
        parsed = _parse_locate_info(payload)
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed["latitude"], 31.4204037, places=5)
        self.assertAlmostEqual(parsed["longitude"], 120.4460565, places=5)
        self.assertEqual(parsed["battery"], 12)
        self.assertFalse(parsed["battery_charging"])
        self.assertTrue(parsed["screen_locked"])
        self.assertEqual(parsed["coordinate_system"], "wgs84")
        self.assertEqual(parsed["network_name"], "tytcp_5G")
        self.assertEqual(parsed["network_type"], "wifi")
        self.assertEqual(parsed["network_signal"], 4)
        self.assertEqual(parsed["network_signal_max"], 4)

    def test_mobile_network_signal_uses_five_bars(self):
        payload = {
            "locateInfo": json.dumps(
                {
                    "latitude_WGS": 31.42,
                    "longitude_WGS": 120.44,
                    "networkInfo": json.dumps(
                        {"name": "中国移动", "signal": "5", "type": "1"}
                    ),
                }
            )
        }
        parsed = _parse_locate_info(payload)
        self.assertEqual(parsed["network_type"], "mobile")
        self.assertEqual(parsed["network_signal"], 5)
        self.assertEqual(parsed["network_signal_max"], 5)

    def test_bd09_top_level_payload_is_converted(self):
        # The top-level latitude/longitude of the API are BD-09.
        payload = {
            "locateInfo": json.dumps(
                {"latitude": 31.424588, "longitude": 120.457152, "accuracy": 20}
            )
        }
        parsed = _parse_locate_info(payload)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["coordinate_system"], "bd09")
        self.assertAlmostEqual(parsed["latitude"], 31.4204, places=3)
        self.assertAlmostEqual(parsed["longitude"], 120.4461, places=3)

    def test_missing_coordinates_returns_none(self):
        self.assertIsNone(_parse_locate_info({"locateInfo": "{}"}))


class MergeResultTests(unittest.TestCase):
    def setUp(self):
        self.previous = {
            "status": "ok",
            "latitude": 31.4,
            "longitude": 120.4,
            "address": "Somewhere",
            "located_at": None,
            "network_name": "tytcp_5G",
            "network_type": "wifi",
            "network_signal": 4,
            "network_signal_max": 4,
        }

    def test_failure_keeps_last_position(self):
        merged = _merge_result(
            self.previous, {"status": "error", "last_error": "Not online"}
        )
        self.assertEqual(merged["latitude"], 31.4)
        self.assertEqual(merged["longitude"], 120.4)
        self.assertEqual(merged["address"], "Somewhere")
        self.assertTrue(merged["stale"])
        self.assertEqual(merged["network_name"], "tytcp_5G")
        self.assertEqual(merged["network_type"], "wifi")
        self.assertEqual(merged["network_signal"], 4)
        self.assertTrue(merged["network_stale"])
        self.assertEqual(merged["status"], "error")
        self.assertIn("last_attempt", merged)

    def test_success_replaces_position(self):
        merged = _merge_result(
            self.previous, {"status": "ok", "latitude": 31.5, "longitude": 120.5}
        )
        self.assertEqual(merged["latitude"], 31.5)
        self.assertFalse(merged["stale"])
        self.assertNotIn("address", merged)

    def test_geocode_failure_keeps_previous_address_when_nearby(self):
        previous = {
            "status": "ok",
            "latitude": 31.4204,
            "longitude": 120.4460,
            "address": "Somewhere",
        }
        merged = _merge_result(
            previous,
            {"status": "ok", "latitude": 31.4205, "longitude": 120.4461},
        )
        self.assertEqual(merged["address"], "Somewhere")
        self.assertTrue(merged["address_stale"])

    def test_geocode_failure_drops_address_when_moved(self):
        previous = {
            "status": "ok",
            "latitude": 31.4204,
            "longitude": 120.4460,
            "address": "Somewhere",
        }
        merged = _merge_result(
            previous,
            {"status": "ok", "latitude": 31.50, "longitude": 120.60},
        )
        self.assertNotIn("address", merged)
        self.assertFalse(merged["address_stale"])

    def test_failure_keeps_address_and_marks_it_stale(self):
        previous = {
            "status": "ok",
            "latitude": 31.4,
            "longitude": 120.4,
            "address": "Somewhere",
        }
        merged = _merge_result(previous, {"status": "error", "last_error": "boom"})
        self.assertEqual(merged["address"], "Somewhere")
        self.assertTrue(merged["address_stale"])
        self.assertTrue(merged["stale"])

    def test_failure_without_cache_does_not_invent_position(self):
        merged = _merge_result({}, {"status": "error", "last_error": "x"})
        self.assertNotIn("latitude", merged)
        self.assertFalse(merged["stale"])


class BatteryMergeTests(unittest.TestCase):
    def _previous(self):
        return {
            "status": "ok",
            "latitude": 31.4,
            "longitude": 120.4,
            "battery": 57,
            "batteries": {
                "left": {"percentage": 57, "stale": False},
                "right": {"percentage": 63, "stale": False},
                "case": {"percentage": None, "stale": False},
            },
        }

    def test_component_missing_from_new_payload_is_inherited_and_stale(self):
        merged = _merge_result(
            self._previous(),
            {
                "status": "ok",
                "latitude": 31.4,
                "longitude": 120.4,
                "batteries": {"left": {"percentage": 50, "stale": False}},
                "battery": 50,
                "battery_stale": False,
            },
        )
        self.assertEqual(merged["batteries"]["left"]["percentage"], 50)
        self.assertEqual(merged["batteries"]["right"]["percentage"], 63)
        self.assertTrue(merged["batteries"]["right"]["stale"])
        self.assertEqual(merged["battery"], 50)
        self.assertTrue(merged["battery_stale"])

    def test_empty_component_keeps_last_known_value(self):
        merged = _merge_result(
            self._previous(),
            {
                "status": "ok",
                "latitude": 31.4,
                "longitude": 120.4,
                "batteries": {
                    "left": {"percentage": 50, "stale": False},
                    "right": {"percentage": None, "stale": False},
                },
            },
        )
        self.assertEqual(merged["batteries"]["right"]["percentage"], 63)
        self.assertTrue(merged["batteries"]["right"]["stale"])

    def test_failure_marks_every_component_stale(self):
        merged = _merge_result(
            self._previous(), {"status": "error", "last_error": "offline"}
        )
        self.assertEqual(merged["batteries"]["left"]["percentage"], 57)
        self.assertTrue(all(item["stale"] for item in merged["batteries"].values()))
        self.assertTrue(merged["battery_stale"])

    def test_phone_merge_keeps_single_main_battery(self):
        previous = {
            "status": "ok",
            "latitude": 31.4,
            "longitude": 120.4,
            "battery": 76,
            "batteries": {"main": {"percentage": 76, "stale": False}},
        }
        merged = _merge_result(
            previous,
            {
                "status": "ok",
                "latitude": 31.4,
                "longitude": 120.4,
                "batteries": {"main": {"percentage": 70, "stale": False}},
            },
        )
        self.assertEqual(merged["battery"], 70)
        self.assertFalse(merged["battery_stale"])


if __name__ == "__main__":
    unittest.main()

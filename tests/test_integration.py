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
        }

    def test_failure_keeps_last_position(self):
        merged = _merge_result(
            self.previous, {"status": "error", "last_error": "Not online"}
        )
        self.assertEqual(merged["latitude"], 31.4)
        self.assertEqual(merged["longitude"], 120.4)
        self.assertEqual(merged["address"], "Somewhere")
        self.assertTrue(merged["stale"])
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


if __name__ == "__main__":
    unittest.main()

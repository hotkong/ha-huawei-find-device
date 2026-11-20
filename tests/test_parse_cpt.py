"""Wearable/earbud locate results (cptLocateInfoList) parsing tests."""

import json
import time
import unittest

from custom_components.huawei_find.api import _parse_locate_info, components_from_locate_result


def _payload(coords, battery="81", exe_result="0"):
    return {
        "locateInfo": "",
        "exeResult": exe_result,
        "executeTime": int(time.time() * 1000),
        "cptLocateInfoList": [
            {
                "cptList": "0",
                "batteryStatus": json.dumps({"percentage": battery}),
                "coordinateInfoList": coords,
            }
        ],
    }


def _earbud_payload():
    """Trimmed copy of a real FreeBuds Lipstick queryLocateResult (2026-11-19)."""
    coords = [
        json.dumps(
            {
                "accuracy": "16.6",
                "latitude": 31.434105363026653,
                "longitude": 120.45295772737263,
                "sysType": "2",
                "time": "1794200959231",
            }
        ),
        json.dumps(
            {
                "accuracy": "16.6",
                "latitude": 31.4299886,
                "longitude": 120.4418233,
                "sysType": "1",
                "time": "1794200959231",
            }
        ),
    ]
    return {
        "locateInfo": "",
        "exeResult": "900",
        "executeTime": 1794200959231,
        "cptLocateInfoList": [
            {
                "cptList": "0",
                "connectType": -1,
                "batteryStatus": "",
                "coordinateInfoList": [],
            },
            {
                "cptList": "1",
                "connectType": -1,
                "batteryStatus": json.dumps({"percentage": "57"}),
                "coordinateInfoList": coords,
            },
            {
                "cptList": "2",
                "connectType": -1,
                "batteryStatus": json.dumps({"percentage": "63"}),
                "coordinateInfoList": coords,
            },
        ],
    }


class CptParseTests(unittest.TestCase):
    def test_prefers_wgs84_entry(self):
        payload = _payload(
            [
                json.dumps({"latitude": 31.329087, "longitude": 120.441357, "sysType": "2", "accuracy": "17.2"}),
                json.dumps({"latitude": 31.3251733, "longitude": 120.4301412, "sysType": "1", "accuracy": "17.2"}),
            ]
        )
        result = _parse_locate_info(payload)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["latitude"], 31.3251733, places=5)
        self.assertAlmostEqual(result["longitude"], 120.4301412, places=5)
        self.assertEqual(result["coordinate_system"], "wgs84")
        self.assertEqual(result["battery"], 81)
        self.assertEqual(result["batteries"]["main"]["percentage"], 81)

    def test_converts_bd09_when_only_bd09(self):
        payload = _payload(
            [json.dumps({"latitude": 31.329087, "longitude": 120.441357, "sysType": "2"})]
        )
        result = _parse_locate_info(payload)
        self.assertIsNotNone(result)
        self.assertEqual(result["coordinate_system"], "bd09")
        self.assertAlmostEqual(result["latitude"], 31.3251733, places=3)
        self.assertAlmostEqual(result["longitude"], 120.4301412, places=3)

    def test_empty_payload(self):
        self.assertIsNone(_parse_locate_info({"locateInfo": ""}))
        self.assertIsNone(_parse_locate_info({}))


class EarbudBatteryTests(unittest.TestCase):
    def test_components_are_kept_separately(self):
        result = _parse_locate_info(_earbud_payload(), per_device_type="1")
        self.assertIsNotNone(result)
        batteries = result["batteries"]
        self.assertEqual(set(batteries), {"case", "left", "right"})
        self.assertEqual(batteries["left"]["percentage"], 57)
        self.assertEqual(batteries["right"]["percentage"], 63)
        self.assertIsNone(batteries["case"]["percentage"])
        # Aggregate = lowest reported value
        self.assertEqual(result["battery"], 57)

    def test_cached_result_is_flagged_stale(self):
        result = _parse_locate_info(_earbud_payload(), per_device_type="1")
        self.assertTrue(result["result_stale"])
        self.assertTrue(result["battery_stale"])
        self.assertEqual(result["result_time"], "2026-11-09T05:09:19.231000+00:00")
        self.assertTrue(all(item["stale"] for item in result["batteries"].values()))

    def test_component_time_is_normalized(self):
        result = _parse_locate_info(_earbud_payload(), per_device_type="1")
        self.assertEqual(
            result["batteries"]["left"]["reported_at"],
            "2026-11-09T05:09:19.231000+00:00",
        )
        self.assertEqual(result["device_time"], "2026-11-09T05:09:19.231000+00:00")

    def test_old_result_without_exe_result_is_still_stale(self):
        payload = _earbud_payload()
        payload.pop("exeResult")
        result = _parse_locate_info(payload, per_device_type="1")
        self.assertFalse(result["result_stale"])
        self.assertTrue(result["battery_stale"])  # component timestamps are 10 days old

    def test_watch_uses_main_component(self):
        result = _parse_locate_info(
            _payload(
                [json.dumps({"latitude": 31.32, "longitude": 120.44, "sysType": "1"})],
                battery="86",
            ),
            per_device_type="2",
        )
        self.assertEqual(result["batteries"]["main"]["percentage"], 86)
        self.assertEqual(result["battery"], 86)
        self.assertFalse(result["battery_stale"])

    def test_unknown_component_keeps_its_number(self):
        payload = _payload(
            [json.dumps({"latitude": 31.32, "longitude": 120.44, "sysType": "1"})]
        )
        payload["cptLocateInfoList"][0]["cptList"] = "9"
        result = _parse_locate_info(payload, per_device_type="1")
        self.assertIn("component_9", result["batteries"])


if __name__ == "__main__":
    unittest.main()


class MultiComponentDetectionTests(unittest.TestCase):
    def test_multiple_entries_are_named_without_per_device_type(self):
        payload = {
            "cptLocateInfoList": [
                {"cptList": "0", "batteryStatus": json.dumps({"percentage": 80})},
                {"cptList": "1", "batteryStatus": json.dumps({"percentage": 60})},
            ]
        }
        self.assertEqual(
            components_from_locate_result(payload, None), ["case", "left"]
        )

    def test_single_entry_stays_main(self):
        payload = {"cptLocateInfoList": [{"cptList": "0", "batteryStatus": ""}]}
        self.assertEqual(components_from_locate_result(payload, "9"), ["main"])

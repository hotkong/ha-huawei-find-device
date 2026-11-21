"""Device list management helper tests."""

import unittest

from custom_components.huawei_find.helpers import build_device_info, scoped_device_id


class ScopedDeviceIdTests(unittest.TestCase):
    def test_scoped_identifier(self):
        self.assertEqual(scoped_device_id("entry", "entry:ABC123"), "ABC123")

    def test_account_hub_is_not_scoped(self):
        self.assertIsNone(scoped_device_id("entry", "account_entry"))

    def test_other_entry(self):
        self.assertIsNone(scoped_device_id("entry", "other:ABC123"))


class DeviceInfoTests(unittest.TestCase):
    def test_firmware_serial_and_model(self):
        info = build_device_info(
            "e1",
            {
                "id": "d1",
                "name": "Phone",
                "model": "ALN-AL00",
                "firmware": "ALN-AL004.2.0.223",
                "serial_number": "FMR0224109023351",
            },
        )
        self.assertEqual(info["manufacturer"], "Huawei")
        self.assertEqual(info["model"], "ALN-AL00")
        self.assertEqual(info["sw_version"], "ALN-AL004.2.0.223")
        self.assertEqual(info["serial_number"], "FMR0224109023351")
        self.assertIn("cloud.huawei.com", info["configuration_url"])

    def test_category_fallback_without_firmware(self):
        info = build_device_info(
            "e1", {"id": "d2", "name": "Tag", "category": "tag"}
        )
        self.assertEqual(info["model"], "HUAWEI Tag")
        self.assertNotIn("sw_version", info)
        self.assertNotIn("serial_number", info)


if __name__ == "__main__":
    unittest.main()

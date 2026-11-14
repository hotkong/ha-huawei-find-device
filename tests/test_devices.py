"""Device list management helper tests."""

import unittest

from custom_components.huawei_find.helpers import scoped_device_id


class ScopedDeviceIdTests(unittest.TestCase):
    def test_scoped_identifier(self):
        self.assertEqual(scoped_device_id("entry", "entry:ABC123"), "ABC123")

    def test_account_hub_is_not_scoped(self):
        self.assertIsNone(scoped_device_id("entry", "account_entry"))

    def test_other_entry(self):
        self.assertIsNone(scoped_device_id("entry", "other:ABC123"))


if __name__ == "__main__":
    unittest.main()

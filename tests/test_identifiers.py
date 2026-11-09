"""Device registry identifier tests."""

import unittest

from custom_components.huawei_find.helpers import device_identifiers
from custom_components.huawei_find.const import DOMAIN


class DeviceIdentifierTests(unittest.TestCase):
    def test_identifier_is_scoped_by_entry(self):
        self.assertEqual(device_identifiers("e1", "d1"), {(DOMAIN, "e1:d1")})
        self.assertNotEqual(
            device_identifiers("e1", "d1"), device_identifiers("e2", "d1")
        )


if __name__ == "__main__":
    unittest.main()

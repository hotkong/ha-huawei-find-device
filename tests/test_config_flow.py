"""Config flow defaults for the automatic refresh interval."""

import unittest

from custom_components.huawei_find.config_flow import _scan_interval_value
from custom_components.huawei_find.const import (
    DEFAULT_SCAN_INTERVAL,
    MAX_SCAN_INTERVAL,
)


class ScanIntervalValueTests(unittest.TestCase):
    def test_missing_value_uses_default(self):
        self.assertEqual(_scan_interval_value(None), DEFAULT_SCAN_INTERVAL)
        self.assertEqual(_scan_interval_value(""), DEFAULT_SCAN_INTERVAL)

    def test_explicit_zero_stays_disabled(self):
        self.assertEqual(_scan_interval_value(0), 0)
        self.assertEqual(_scan_interval_value("0"), 0)

    def test_values_are_clamped(self):
        self.assertEqual(_scan_interval_value(30), 30)
        self.assertEqual(_scan_interval_value(-10), 0)
        self.assertEqual(_scan_interval_value(10_000), MAX_SCAN_INTERVAL)

    def test_unparsable_value_falls_back_to_default(self):
        self.assertEqual(_scan_interval_value("bad"), DEFAULT_SCAN_INTERVAL)


if __name__ == "__main__":
    unittest.main()

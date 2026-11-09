"""Session age helper tests."""

import unittest
from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.huawei_find.options import auth_age_days


class AuthAgeTests(unittest.TestCase):
    def test_age_in_days(self):
        entry = SimpleNamespace(
            data={"auth_time": (dt_util.utcnow() - timedelta(days=2)).isoformat()}
        )
        self.assertAlmostEqual(auth_age_days(entry), 2.0, places=1)

    def test_missing_auth_time(self):
        self.assertIsNone(auth_age_days(SimpleNamespace(data={})))

    def test_invalid_auth_time(self):
        entry = SimpleNamespace(data={"auth_time": "not-a-date"})
        self.assertIsNone(auth_age_days(entry))


if __name__ == "__main__":
    unittest.main()

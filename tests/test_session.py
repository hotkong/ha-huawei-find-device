"""Session age helper tests."""

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.util import dt as dt_util

from custom_components.huawei_find import options
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


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_clears_the_reauth_latch(self):
        runtime = SimpleNamespace(
            client=SimpleNamespace(async_heartbeat=AsyncMock()),
            heartbeat_failures=1,
            heartbeat_ok=False,
            last_heartbeat=None,
            reauth_requested=True,
            expiry_warned=False,
            entry=SimpleNamespace(entry_id="entry-1", data={}),
        )
        with patch.object(options.persistent_notification, "async_dismiss"), \
             patch.object(options, "_check_expiry_reminder"), \
             patch.object(options, "async_dispatcher_send"):
            await options._async_heartbeat(SimpleNamespace(), runtime)

        self.assertFalse(runtime.reauth_requested)
        self.assertTrue(runtime.heartbeat_ok)


if __name__ == "__main__":
    unittest.main()

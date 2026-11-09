"""Retry policy and option parsing tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.huawei_find.coordinator import _async_locate_with_retry
from custom_components.huawei_find.helpers import _interval_minutes
from custom_components.huawei_find.api import (
    HuaweiFindAuthError,
    HuaweiFindDeviceError,
    HuaweiFindError,
)


def _runtime(exc):
    client = SimpleNamespace(
        async_locate_and_wait=AsyncMock(side_effect=exc)
    )
    return SimpleNamespace(client=client)


class RetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_error_is_not_retried(self):
        runtime = _runtime(HuaweiFindDeviceError("device inactive"))
        with self.assertRaises(HuaweiFindDeviceError):
            await _async_locate_with_retry(runtime, {})
        self.assertEqual(runtime.client.async_locate_and_wait.await_count, 1)

    async def test_auth_error_is_not_retried(self):
        runtime = _runtime(HuaweiFindAuthError("401"))
        with self.assertRaises(HuaweiFindAuthError):
            await _async_locate_with_retry(runtime, {})
        self.assertEqual(runtime.client.async_locate_and_wait.await_count, 1)

    async def test_transient_error_is_retried_once(self):
        runtime = _runtime(HuaweiFindError("timeout"))
        with patch("custom_components.huawei_find.coordinator.LOCATE_RETRY_DELAY", 0):
            with self.assertRaises(HuaweiFindError):
                await _async_locate_with_retry(runtime, {})
        self.assertEqual(runtime.client.async_locate_and_wait.await_count, 2)

    async def test_success_returns_immediately(self):
        client = SimpleNamespace(
            async_locate_and_wait=AsyncMock(return_value={"status": "ok"})
        )
        result = await _async_locate_with_retry(SimpleNamespace(client=client), {})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(client.async_locate_and_wait.await_count, 1)


class IntervalTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(_interval_minutes(15), 15)
        self.assertEqual(_interval_minutes("20"), 20)
        self.assertEqual(_interval_minutes(None), 0)
        self.assertEqual(_interval_minutes(""), 0)
        self.assertEqual(_interval_minutes("bad"), 0)
        self.assertEqual(_interval_minutes(-5), 0)


if __name__ == "__main__":
    unittest.main()

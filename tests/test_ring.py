"""Ring (find my phone) payload and cooldown tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.huawei_find.coordinator import async_ring_device
from custom_components.huawei_find.helpers import supports_ring
from custom_components.huawei_find.api import (
    HuaweiFindClient,
    HuaweiFindDeviceError,
)


class BellPayloadTests(unittest.TestCase):
    def test_own_device_payload(self):
        client = HuaweiFindClient("A=1")
        payload = client._bell_payload({"id": "d1", "device_type": 9})
        self.assertEqual(payload["deviceId"], "d1")
        self.assertEqual(payload["deviceType"], 9)
        self.assertEqual(payload["perDeviceType"], "0")
        self.assertNotIn("senderUserId", payload)

    def test_shared_device_payload(self):
        client = HuaweiFindClient("A=1")
        payload = client._bell_payload(
            {"id": "d1", "device_type": 9, "is_shared": True, "sender_user_id": "u1"}
        )
        self.assertEqual(payload["senderUserId"], "u1")
        self.assertEqual(payload["relationType"], 2)


def _runtime(ring_result=True):
    return SimpleNamespace(
        devices={"d1": {"id": "d1", "name": "Test", "per_device_type": "0"}},
        last_ring={},
        ring_results={},
        client=SimpleNamespace(async_ring=AsyncMock(return_value=ring_result)),
        entry=SimpleNamespace(entry_id="e1"),
    )


class RingCapabilityTests(unittest.TestCase):
    def test_phone_can_ring(self):
        self.assertTrue(supports_ring({"per_device_type": "0", "is_shared": False}))

    def test_watch_cannot_ring(self):
        self.assertFalse(supports_ring({"per_device_type": "2", "is_shared": False}))

    def test_shared_cannot_ring(self):
        self.assertFalse(supports_ring({"per_device_type": "0", "is_shared": True}))


class RingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ring_succeeds_once_per_cooldown(self):
        runtime = _runtime()
        with patch("custom_components.huawei_find.coordinator.async_dispatcher_send"):
            first = await async_ring_device(SimpleNamespace(), runtime, "d1")
            second = await async_ring_device(SimpleNamespace(), runtime, "d1")
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("wait", second["error"].lower())
        self.assertEqual(runtime.client.async_ring.await_count, 1)

    async def test_shared_device_is_rejected(self):
        runtime = _runtime()
        runtime.devices = {"d1": {"id": "d1", "name": "Family", "is_shared": True}}
        runtime.devices["d1"]["per_device_type"] = "0"
        result = await async_ring_device(SimpleNamespace(), runtime, "d1")
        self.assertFalse(result["ok"])
        self.assertIn("only supported", result["error"])
        runtime.client.async_ring.assert_not_called()

    async def test_device_error_is_reported(self):
        runtime = _runtime()
        runtime.client.async_ring = AsyncMock(
            side_effect=HuaweiFindDeviceError("device inactive")
        )
        result = await async_ring_device(SimpleNamespace(), runtime, "d1")
        self.assertFalse(result["ok"])
        self.assertIn("inactive", result["error"])


if __name__ == "__main__":
    unittest.main()

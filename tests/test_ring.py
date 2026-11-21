"""Ring (find my phone) payload and cooldown tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.huawei_find.coordinator import async_ring_device
from custom_components.huawei_find.helpers import supports_ring, supports_stop_ring
from custom_components.huawei_find.api import (
    HuaweiFindClient,
    HuaweiFindDeviceError,
    HuaweiFindError,
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
        ring_errors={},
        client=SimpleNamespace(async_ring=AsyncMock(return_value=ring_result)),
        entry=SimpleNamespace(entry_id="e1"),
    )


class LocatePayloadTests(unittest.TestCase):
    def test_watch_payload_uses_its_own_per_device_type(self):
        client = HuaweiFindClient("A=1")
        payload = client._locate_payload(
            {"id": "d1", "device_type": -1, "per_device_type": "2"}
        )
        self.assertEqual(payload["perDeviceType"], "2")
        self.assertEqual(payload["deviceType"], -1)

    def test_phone_payload_defaults_to_zero(self):
        client = HuaweiFindClient("A=1")
        payload = client._locate_payload({"id": "d1", "device_type": 9})
        self.assertEqual(payload["perDeviceType"], "0")


class ChargingCapabilityTests(unittest.TestCase):
    def test_phone_has_charging(self):
        from custom_components.huawei_find.helpers import supports_charging
        self.assertTrue(supports_charging({"per_device_type": "0"}))

    def test_shared_phone_has_charging(self):
        from custom_components.huawei_find.helpers import supports_charging
        self.assertTrue(supports_charging({"per_device_type": ""}))

    def test_wearable_has_no_charging(self):
        from custom_components.huawei_find.helpers import supports_charging
        self.assertFalse(supports_charging({"per_device_type": "2"}))

    def test_earbuds_have_no_charging(self):
        from custom_components.huawei_find.helpers import supports_charging
        self.assertFalse(supports_charging({"per_device_type": "1"}))

    def test_tag_has_no_charging(self):
        from custom_components.huawei_find.helpers import supports_charging
        self.assertFalse(supports_charging({"per_device_type": "3"}))


class StopRingCapabilityTests(unittest.TestCase):
    def test_phone_has_no_stop(self):
        self.assertFalse(supports_stop_ring({"per_device_type": "0", "is_shared": False}))

    def test_watch_has_stop(self):
        self.assertTrue(supports_stop_ring({"per_device_type": "2", "is_shared": False}))

    def test_tag_has_stop(self):
        self.assertTrue(supports_stop_ring({"per_device_type": "3", "is_shared": False}))

    def test_bell_earbuds_have_stop(self):
        self.assertTrue(supports_stop_ring({"per_device_type": "1", "capability": ["bell"], "is_shared": False}))

    def test_shared_has_no_stop(self):
        self.assertFalse(supports_stop_ring({"per_device_type": "2", "is_shared": True}))


class RingCapabilityTests(unittest.TestCase):
    def test_phone_can_ring(self):
        self.assertTrue(supports_ring({"per_device_type": "0", "is_shared": False}))

    def test_watch_can_ring_when_owned(self):
        self.assertTrue(supports_ring({"per_device_type": "2", "is_shared": False}))

    def test_band_can_ring_when_owned(self):
        self.assertTrue(supports_ring({"per_device_type": "2", "is_shared": False}))

    def test_tag_can_ring_when_owned(self):
        self.assertTrue(supports_ring({"per_device_type": "3", "is_shared": False}))

    def test_shared_tag_cannot_ring(self):
        self.assertFalse(supports_ring({"per_device_type": "3", "is_shared": True}))

    def test_shared_cannot_ring(self):
        self.assertFalse(supports_ring({"per_device_type": "0", "is_shared": True}))

    def test_earbuds_with_bell_can_ring(self):
        self.assertTrue(
            supports_ring(
                {
                    "per_device_type": "1",
                    "is_shared": False,
                    "capability": ["bell"],
                }
            )
        )

    def test_earbuds_without_bell_cannot_ring(self):
        self.assertFalse(
            supports_ring(
                {"per_device_type": "1", "is_shared": False, "capability": []}
            )
        )

    def test_shared_earbuds_cannot_ring(self):
        self.assertFalse(
            supports_ring(
                {
                    "per_device_type": "1",
                    "is_shared": True,
                    "capability": ["bell"],
                }
            )
        )


class RingTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_failure_is_retried_once(self):
        runtime = _runtime()
        runtime.client.async_ring = AsyncMock(
            side_effect=[HuaweiFindError("Ring failed: Send push failed"), True]
        )
        with patch("custom_components.huawei_find.coordinator.async_dispatcher_send"), \
             patch("custom_components.huawei_find.coordinator.asyncio.sleep", AsyncMock()):
            result = await async_ring_device(SimpleNamespace(), runtime, "d1")
        self.assertTrue(result["ok"])
        self.assertEqual(runtime.client.async_ring.await_count, 2)

    async def test_other_api_error_is_not_retried(self):
        runtime = _runtime()
        runtime.client.async_ring = AsyncMock(
            side_effect=HuaweiFindError("Ring failed: code=1001")
        )
        result = await async_ring_device(SimpleNamespace(), runtime, "d1")
        self.assertFalse(result["ok"])
        self.assertEqual(runtime.client.async_ring.await_count, 1)

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

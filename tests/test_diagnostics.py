"""Tests for the redacted diagnostics snapshot."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.huawei_find import diagnostics as diag

RAW_COOKIE = "JSESSIONID=secret; CSRFToken=t; USER_ID=123"

RESULT = {
    "status": "ok",
    "stale": False,
    "last_error": None,
    "latitude": 31.4299886,
    "longitude": 120.4418233,
    "accuracy": 16.6,
    "coordinate_system": "wgs84",
    "device_time": "2026-11-09T05:09:19+00:00",
    "result_time": "2026-11-09T05:09:19+00:00",
    "result_stale": True,
    "located_at": None,
    "last_attempt": None,
    "battery": 57,
    "battery_stale": True,
    "batteries": {
        "left": {"percentage": 57, "stale": True},
        "right": {"percentage": 63, "stale": True},
        "case": {"percentage": None, "stale": True},
    },
    "address": "江苏省苏州市相城区某小区 1 幢",
}


def _runtime():
    return SimpleNamespace(
        entry=SimpleNamespace(
            entry_id="e1",
            title="Huawei (5311)",
            options={"scan_interval": 15},
            data={"cookie": RAW_COOKIE, "auth_time": "2026-11-20T06:00:00+00:00"},
            state="loaded",
        ),
        geocode_provider="auto",
        amap_key="amap-secret",
        browser_url="http://browser:3000",
        browser_token="browser-secret",
        browser_username="13900000000",
        browser_password="password-secret",
        heartbeat_ok=True,
        heartbeat_failures=0,
        last_heartbeat=None,
        reauth_requested=False,
        expiry_warned=False,
        login_error="verification required",
        last_login_success=None,
        devices={
            "ABCDEF123456": {
                "id": "ABCDEF123456",
                "name": "FreeBuds",
                "model": "FreeBuds Lipstick",
                "per_device_type": "1",
                "components": ["case", "left", "right"],
                "capability": ["bell"],
                "is_shared": False,
            }
        },
        results={"ABCDEF123456": RESULT},
        refreshing=set(),
        ring_errors={},
    )


class SanitizeTests(unittest.TestCase):
    def test_address_removed_and_coordinates_rounded(self):
        payload = diag.sanitize_result(RESULT)
        self.assertNotIn("address", payload)
        self.assertEqual(payload["latitude"], 31.43)
        self.assertEqual(payload["longitude"], 120.44)
        self.assertEqual(payload["battery"], 57)
        self.assertEqual(payload["batteries"]["left"]["percentage"], 57)

    def test_device_id_is_hashed(self):
        summary = diag.device_summary({"id": "ABCDEF123456", "name": "x"})
        self.assertNotIn("ABCDEF123456", summary["id_hash"])
        self.assertEqual(len(summary["id_hash"]), 12)


class DiagnosticsPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_secrets_in_payload(self):
        runtime = _runtime()
        entry = runtime.entry
        hass = SimpleNamespace(data={"huawei_find": {"e1": runtime}})
        with patch.object(
            diag, "async_get_integration", AsyncMock(return_value=SimpleNamespace(version="0.6.0"))
        ):
            payload = await diag.async_get_config_entry_diagnostics(hass, entry)

        blob = json.dumps(payload, ensure_ascii=False)
        for secret in (
            RAW_COOKIE,
            "amap-secret",
            "browser-secret",
            "password-secret",
            "13900000000",
            "某小区",
            "ABCDEF123456",
        ):
            self.assertNotIn(secret, blob)
        self.assertIn('"latitude": 31.43', blob)
        self.assertEqual(payload["session"]["cookie_length"], len(RAW_COOKIE))
        self.assertEqual(payload["devices"][0]["result"]["battery"], 57)

    async def test_runtime_missing_is_tolerated(self):
        entry = SimpleNamespace(entry_id="e2", title="x", options={}, data={}, state="setup_error")
        hass = SimpleNamespace(data={"huawei_find": {}})
        with patch.object(
            diag, "async_get_integration", AsyncMock(return_value=SimpleNamespace(version="0.6.0"))
        ):
            payload = await diag.async_get_config_entry_diagnostics(hass, entry)
        self.assertEqual(payload["devices"], [])
        self.assertEqual(payload["options"], {})


if __name__ == "__main__":
    unittest.main()

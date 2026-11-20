"""Tests for automatic re-login orchestration (browser fallback only)."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.huawei_find import auto_login as al


def _runtime():
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id="e1", title="T", unique_id="huawei_1", data={}),
        client=SimpleNamespace(set_cookie=Mock()),
        browser_url="http://browser:3000",
        browser_token="",
        browser_username="user",
        browser_password="pass",
        login_lock=asyncio.Lock(),
        last_login_attempt=0.0,
        login_error=None,
        reauth_requested=False,
        expiry_warned=False,
        last_login_success=None,
    )


class AutoLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_configured_fails_fast(self):
        runtime = _runtime()
        runtime.browser_url = ""
        outcome = await al.async_try_auto_login(SimpleNamespace(), runtime, reason="test")
        self.assertEqual(outcome, al.FAILED)

    async def test_browser_recovers(self):
        runtime = _runtime()
        with patch.object(
            al, "async_try_browser_login", AsyncMock(return_value=True)
        ) as login:
            outcome = await al.async_try_auto_login(SimpleNamespace(), runtime, reason="test")
        self.assertEqual(outcome, al.RECOVERED)
        login.assert_called_once()

    async def test_browser_failure_is_reported(self):
        runtime = _runtime()
        with patch.object(
            al, "async_try_browser_login", AsyncMock(return_value=False)
        ):
            outcome = await al.async_try_auto_login(SimpleNamespace(), runtime, reason="test", force=True)
        self.assertEqual(outcome, al.FAILED)

    async def test_busy_login_is_skipped(self):
        runtime = _runtime()
        await runtime.login_lock.acquire()
        outcome = await al.async_try_auto_login(SimpleNamespace(), runtime, reason="test", force=True)
        self.assertEqual(outcome, al.SKIPPED)
        runtime.login_lock.release()

    async def test_cooldown_after_success_is_skipped(self):
        runtime = _runtime()
        runtime.last_login_attempt = asyncio.get_event_loop().time()
        with patch.object(al, "async_try_browser_login", AsyncMock(return_value=True)) as login:
            outcome = await al.async_try_auto_login(SimpleNamespace(), runtime, reason="test")
        self.assertEqual(outcome, al.SKIPPED)
        login.assert_not_called()

    async def test_cooldown_after_failure_prompts(self):
        runtime = _runtime()
        runtime.last_login_attempt = asyncio.get_event_loop().time()
        runtime.login_error = "verification required"
        outcome = await al.async_try_auto_login(SimpleNamespace(), runtime, reason="test")
        self.assertEqual(outcome, al.FAILED)


if __name__ == "__main__":
    unittest.main()

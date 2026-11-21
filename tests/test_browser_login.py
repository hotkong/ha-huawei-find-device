"""Tests for the optional browser-login fallback."""

import unittest

from custom_components.huawei_find.api import cookie_is_usable, cookie_matches_entry
from custom_components.huawei_find.browser_login import (
    build_login_code,
    cooldown_remaining,
)


class LoginCodeTests(unittest.TestCase):
    def test_credentials_are_json_quoted(self):
        code = build_login_code('139"0000', "p@ss\\word")
        self.assertIn('const USER = "139\\"0000";', code)
        self.assertIn('const PASSWORD = "p@ss\\\\word";', code)
        self.assertNotIn("__USERNAME__", code)
        self.assertNotIn("__PASSWORD__", code)

    def test_code_targets_huawei_login_frame(self):
        code = build_login_code("u", "p")
        self.assertIn("cloud.huawei.com", code)
        self.assertIn("id1.cloud.huawei.com", code)
        self.assertIn("USER_ID", code)


class CookieUsabilityTests(unittest.TestCase):
    def test_complete_cookie_is_usable(self):
        self.assertTrue(
            cookie_is_usable("JSESSIONID=x; CSRFToken=t; USER_ID=123; other=1")
        )

    def test_csrf_alias_is_usable(self):
        self.assertTrue(
            cookie_is_usable("HWWAFSESID=x; hicloud_csrf=t; userId=123")
        )

    def test_missing_parts_are_rejected(self):
        self.assertFalse(cookie_is_usable("JSESSIONID=x; USER_ID=123"))
        self.assertFalse(cookie_is_usable("CSRFToken=t; USER_ID=123"))
        self.assertFalse(cookie_is_usable(""))


class CooldownTests(unittest.TestCase):
    def test_first_attempt_is_allowed(self):
        self.assertEqual(cooldown_remaining(0.0, now=1000.0), 0)

    def test_never_attempted_is_allowed_on_a_fresh_boot(self):
        # A newly started process has a small monotonic clock; 0.0 is "never
        # attempted", not "attempted at boot".
        self.assertEqual(cooldown_remaining(0.0, now=5.0), 0)

    def test_recent_attempt_is_blocked(self):
        self.assertEqual(cooldown_remaining(1000.0, now=1300.0), 300)

    def test_cooldown_expires(self):
        self.assertEqual(cooldown_remaining(1000.0, now=1600.0), 0)


if __name__ == "__main__":
    unittest.main()


import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.huawei_find import browser_login as bl


def _runtime():
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id="e1", title="Test", unique_id="huawei_1", data={"cookie": "old"}),
        client=SimpleNamespace(set_cookie=Mock()),
        browser_url="http://browser:3000",
        browser_token="",
        browser_username="user",
        browser_password="pass",
        login_lock=asyncio.Lock(),
        last_login_attempt=0.0,
        last_login_success=None,
        login_error="old error",
        reauth_requested=True,
        expiry_warned=True,
    )


def _hass():
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=Mock()),
    )


class TryBrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_applies_cookie_and_clears_state(self):
        runtime = _runtime()
        hass = _hass()
        cookie = "JSESSIONID=x; CSRFToken=t; USER_ID=1"
        with patch.object(bl, "async_browser_login", AsyncMock(return_value=cookie)), \
             patch.object(bl.persistent_notification, "async_dismiss"):
            ok = await bl.async_try_browser_login(hass, runtime, "test", force=True)
        self.assertTrue(ok)
        runtime.client.set_cookie.assert_called_once_with(cookie)
        hass.config_entries.async_update_entry.assert_called_once()
        self.assertFalse(runtime.reauth_requested)
        self.assertIsNone(runtime.login_error)

    async def test_failure_keeps_reauth_state(self):
        runtime = _runtime()
        hass = _hass()
        with patch.object(
            bl, "async_browser_login", AsyncMock(side_effect=bl.BrowserLoginError("verification required"))
        ):
            ok = await bl.async_try_browser_login(hass, runtime, "test", force=True)
        self.assertFalse(ok)
        self.assertIn("verification required", runtime.login_error)
        self.assertTrue(runtime.reauth_requested)
        runtime.client.set_cookie.assert_not_called()

    async def test_cooldown_skips_second_attempt(self):
        runtime = _runtime()
        runtime.last_login_attempt = asyncio.get_event_loop().time()
        hass = _hass()
        with patch.object(bl, "async_browser_login", AsyncMock()) as login:
            ok = await bl.async_try_browser_login(hass, runtime, "test")
        self.assertFalse(ok)
        login.assert_not_called()

    async def test_cookie_for_other_account_is_rejected(self):
        runtime = _runtime()
        hass = _hass()
        other = "JSESSIONID=x; CSRFToken=t; USER_ID=999"
        with patch.object(bl, "async_browser_login", AsyncMock(return_value=other)):
            ok = await bl.async_try_browser_login(hass, runtime, "test", force=True)
        self.assertFalse(ok)
        self.assertIn("different Huawei account", runtime.login_error or "")
        runtime.client.set_cookie.assert_not_called()

    async def test_not_configured_returns_false(self):
        runtime = _runtime()
        runtime.browser_url = ""
        self.assertFalse(
            await bl.async_try_browser_login(_hass(), runtime, "test", force=True)
        )


class CookieOwnershipTests(unittest.TestCase):
    def test_matching_account(self):
        self.assertTrue(
            cookie_matches_entry("JSESSIONID=x; CSRFToken=t; USER_ID=42", "huawei_42")
        )

    def test_other_account(self):
        self.assertFalse(
            cookie_matches_entry("JSESSIONID=x; CSRFToken=t; USER_ID=41", "huawei_42")
        )

    def test_unknown_entry_id_is_not_blocked(self):
        self.assertTrue(cookie_matches_entry("JSESSIONID=x; CSRFTOKEN=t; USER_ID=1", None))

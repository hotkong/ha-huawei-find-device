"""Cookie handling tests (no network)."""

import unittest
from unittest.mock import MagicMock

from custom_components.huawei_find.api import HuaweiFindClient, HuaweiFindError


class FakeResponse:
    def __init__(self, set_cookies):
        self.headers = MagicMock()
        self.headers.getall.return_value = set_cookies


class CookieAbsorbTests(unittest.TestCase):
    def test_rotation_is_merged(self):
        client = HuaweiFindClient("A=1; B=2")
        client._absorb_set_cookie(FakeResponse(["A=9; Path=/"]))
        self.assertEqual(client._cookies["A"], "9")
        self.assertEqual(client._cookies["B"], "2")

    def test_deletion_is_honoured(self):
        client = HuaweiFindClient("A=1; B=2")
        client._absorb_set_cookie(FakeResponse(["A=; Max-Age=0; Path=/"]))
        self.assertNotIn("A", client._cookies)
        self.assertEqual(client._cookies["B"], "2")

    def test_set_cookie_replaces_session(self):
        client = HuaweiFindClient("A=1")
        client.set_cookie("CSRFToken=csrf; userId=42")
        self.assertEqual(client.csrf_token, "csrf")
        self.assertEqual(client.user_id, "42")
        self.assertIn("CSRFToken", client._cookies)

    def test_set_cookie_rejects_empty(self):
        client = HuaweiFindClient("A=1")
        with self.assertRaises(HuaweiFindError):
            client.set_cookie("   ")

    def test_empty_value_without_deletion_keeps_cookie(self):
        client = HuaweiFindClient("A=1")
        client._absorb_set_cookie(FakeResponse(["A="]))
        self.assertEqual(client._cookies["A"], "1")

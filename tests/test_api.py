"""Cookie handling tests (no network)."""

import unittest
from unittest.mock import MagicMock

from custom_components.huawei_find.api import HuaweiFindClient


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

    def test_empty_value_without_deletion_keeps_cookie(self):
        client = HuaweiFindClient("A=1")
        client._absorb_set_cookie(FakeResponse(["A="]))
        self.assertEqual(client._cookies["A"], "1")

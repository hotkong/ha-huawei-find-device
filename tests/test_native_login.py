"""Behaviour of the browserless-free account sign-in.

The account gateway only issues a full ticket when ``dev`` registers a device
first, so the login sends a fingerprint. These tests pin what the integration
relies on: the fingerprint decodes back to a verified parameter list, base64
padding is kept (the gateway refuses a stripped value), a first sign-in carries
no device id so the gateway allocates one, a stored id survives a refresh, and a
damaged value fails instead of silently presenting a different device.

They also cover the pieces that never touch the network: the OAuth redirect the
``ajax/login`` reply must lead to, the local attempt cooldown, and the promise
that the password cannot appear in a log line.
"""

import inspect
import re
import unittest

from custom_components.huawei_find import auth, fingerprint

DEVICE_ID = "0123456789abcdef" * 5 + "0123"


class FingerprintTests(unittest.TestCase):
    def test_first_signin_carries_no_device_id(self):
        params = fingerprint.unseal(fingerprint.mint())

        self.assertEqual(len(params), 21)
        self.assertNotIn("localStorage", params)
        for field in ("canvas", "fonts", "webgl"):
            self.assertEqual(len(params[field]), 40)

    def test_value_keeps_its_base64_padding(self):
        # The gateway refuses a value whose base64 padding was stripped, so the
        # length has to stay a multiple of four (padding present when needed).
        for value in (fingerprint.mint(), fingerprint.mint(DEVICE_ID)):
            with self.subTest(length=len(value)):
                self.assertEqual(len(value) % 4, 0)

    def test_mint_carries_the_given_device_id(self):
        params = fingerprint.unseal(fingerprint.mint(DEVICE_ID))
        self.assertEqual(params["localStorage"], DEVICE_ID)

    def test_mint_rejects_a_device_id_of_the_wrong_shape(self):
        for bad in ("", "abc", "z" * 84, DEVICE_ID[:-1]):
            with self.subTest(value=bad[:8]):
                with self.assertRaises(ValueError):
                    fingerprint.mint(bad)

    def test_two_mints_differ_in_their_device_hashes(self):
        first = fingerprint.unseal(fingerprint.mint())
        second = fingerprint.unseal(fingerprint.mint())
        self.assertNotEqual(first["canvas"], second["canvas"])
        self.assertNotEqual(first["fonts"], second["fonts"])

    def test_checksum_is_what_unseal_checks(self):
        value = fingerprint.mint(DEVICE_ID)
        self.assertEqual(fingerprint.seal(fingerprint.unseal(value)), value)

    def test_device_id_helpers(self):
        self.assertTrue(fingerprint.valid_device_id(DEVICE_ID))
        self.assertFalse(fingerprint.valid_device_id(None))
        self.assertFalse(fingerprint.valid_device_id(DEVICE_ID[:-1]))
        self.assertEqual(fingerprint.device_id_of(fingerprint.mint(DEVICE_ID)), DEVICE_ID)
        self.assertIsNone(fingerprint.device_id_of(fingerprint.mint()))


class FingerprintRefreshTests(unittest.TestCase):
    def test_refresh_keeps_the_device_id_and_hashes(self):
        stored = fingerprint.mint(DEVICE_ID)
        before = fingerprint.unseal(stored)
        after = fingerprint.unseal(fingerprint.fresh(stored))

        self.assertEqual(after["localStorage"], before["localStorage"])
        self.assertEqual(after["canvas"], before["canvas"])
        self.assertGreaterEqual(int(after["ett"]), int(before["ett"]))

    def test_damaged_value_fails_instead_of_switching_devices(self):
        for broken in ("", None, "not-a-fingerprint", "AAAA", fingerprint.mint()[:-6]):
            with self.subTest(broken=str(broken)[:12]):
                with self.assertRaises(ValueError):
                    fingerprint.fresh(broken)

    def test_tampered_checksum_is_rejected(self):
        with self.assertRaises(ValueError):
            fingerprint.unseal("AAAA")


class OAuthRedirectTests(unittest.TestCase):
    def test_absolute_redirect_is_used_as_is(self):
        url = "https://cloud.huawei.com:443/homeTransit?code=1ST-1-abc&state=x"
        self.assertEqual(auth._oauth_redirect_target({"code": url}), url)

    def test_bare_code_is_wrapped_in_the_redirect_uri(self):
        target = auth._oauth_redirect_target({"code": "0Q5x.y/z"})
        self.assertEqual(
            target, f"{auth.OAUTH_REDIRECT_URI}?code=0Q5x.y%2Fz"
        )

    def test_other_redirect_fields_are_accepted(self):
        target = auth._oauth_redirect_target(
            {"callbackURL": "https://cloud.huawei.com:443/others/login.action?ticket=1"}
        )
        self.assertTrue(target.startswith("https://cloud.huawei.com:443/others/login.action"))

    def test_missing_redirect_is_reported(self):
        for payload in ({}, {"code": ""}, {"code": "   "}, None, [], {"code": 42}):
            with self.subTest(payload=payload):
                self.assertIsNone(auth._oauth_redirect_target(payload))

    def test_a_redirect_in_an_unexpected_field_is_still_followed(self):
        url = "https://cloud.huawei.com:443/homeTransit?code=1ST-1-abc"
        target = auth._oauth_redirect_target({"isSuccess": 1, "targetUrl": url})
        self.assertEqual(target, url)

    def test_short_values_are_never_mistaken_for_a_code(self):
        for payload in ({"isSuccess": "1"}, {"isSuccess": 1, "logID": "abc"}):
            with self.subTest(payload=payload):
                self.assertIsNone(auth._oauth_redirect_target(payload))

    def test_the_log_names_the_field_and_never_the_value(self):
        secret = "1ST-9-" + "z" * 40
        with self.assertLogs("custom_components.huawei_find.auth", level="WARNING") as logs:
            target = auth._oauth_redirect_target({"oddName": secret})
        self.assertIsNotNone(target)
        joined = "\n".join(logs.output)
        self.assertIn("oddName", joined)
        self.assertNotIn(secret, joined)

    def test_an_error_redirect_is_never_followed(self):
        error = (
            "https://cloud.huawei.com:443/homeTransit?error=1107"
            "&error_description=access+denied&ticket=1ST-9-secret"
        )
        self.assertIsNone(auth._oauth_redirect_target({"errorRedirect": error}))

    def test_the_error_is_reported_without_its_url_or_ticket(self):
        error = (
            "https://cloud.huawei.com:443/homeTransit?error=1107"
            "&error_description=access+denied&ticket=1ST-9-secret"
        )
        message = auth._oauth_error({"errorRedirect": error, "isSuccess": 0})
        self.assertIn("error_description=access denied", message)
        self.assertNotIn("1ST-9-secret", message)
        self.assertNotIn("https://", message)

    def test_a_successful_reply_has_no_error(self):
        for payload in ({"code": "0Q5x.y/z"}, {"errorCode": "0"}, {}, None):
            with self.subTest(payload=payload):
                self.assertIsNone(auth._oauth_error(payload))

    def test_the_oauth_request_asks_only_for_the_allowed_scope(self):
        self.assertEqual(auth.OAUTH_SCOPE, "openid")

    def test_only_the_portal_itself_counts_as_finished(self):
        finished = (
            "https://cloud.huawei.com/home",
            "https://cloud.huawei.com:443/homeTransit?code=abc",
        )
        not_finished = (
            "https://oauth-login1.cloud.huawei.com/oauth2/v3/loginCallback?code_challenge=x",
            "https://id1.cloud.huawei.com/CAS/portal/loginAuth.html?reqClientType=90",
            "https://cloud.huawei.com.example.net/home",
        )
        for url in finished:
            with self.subTest(url=url):
                self.assertTrue(auth._landed_on_portal(url))
        for url in not_finished:
            with self.subTest(url=url):
                self.assertFalse(auth._landed_on_portal(url))


class AttemptCooldownTests(unittest.TestCase):
    def setUp(self):
        auth._attempts.clear()

    def tearDown(self):
        auth._attempts.clear()

    def test_attempts_are_limited_and_never_lock_the_account_silently(self):
        for _ in range(auth._ATTEMPT_LIMIT):
            auth._record_attempt("13700000000")
        with self.assertRaises(auth.HuaweiLoginThrottled):
            auth._record_attempt("13700000000")

    def test_throttle_is_a_verification_error_not_a_wrong_password(self):
        self.assertTrue(issubclass(auth.HuaweiLoginThrottled, auth.HuaweiLoginCodeRequired))

    def test_accounts_are_counted_separately(self):
        for _ in range(auth._ATTEMPT_LIMIT):
            auth._record_attempt("13700000000")
        auth._record_attempt("13700000001")


class DeviceRegistrationTests(unittest.TestCase):
    """The bridge client type only knows the ids registered for itself."""

    class _Client:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = []

        async def ajax(self, action, values, **kwargs):
            self.calls.append(values.get("fp", ""))
            return self.replies.pop(0)

    def _run(self, replies, device_id):
        import asyncio

        client = self._Client(replies)
        issued = asyncio.run(
            auth._register_device(client, {"reqClientType": "90"}, {}, device_id)
        )
        return issued, client.calls

    def test_known_device_is_reused(self):
        issued, calls = self._run([{"isSuccess": 1, "sid": DEVICE_ID}], DEVICE_ID)
        self.assertEqual(issued, DEVICE_ID)
        self.assertEqual(len(calls), 1)

    def test_unknown_device_gets_one_for_this_client_type(self):
        fresh = "b" * 84
        issued, calls = self._run(
            [{"isSuccess": 1, "sid": ""}, {"isSuccess": 1, "sid": fresh}], DEVICE_ID
        )
        self.assertEqual(issued, fresh)
        self.assertEqual(len(calls), 2)
        # The first attempt carries the known id, the retry deliberately carries
        # none so the gateway allocates one for this client type.
        self.assertEqual(fingerprint.device_id_of(calls[0]), DEVICE_ID)
        self.assertIsNone(fingerprint.device_id_of(calls[1]))

    def test_first_registration_asks_for_an_id(self):
        fresh = "c" * 84
        issued, calls = self._run([{"isSuccess": 1, "sid": fresh}], None)
        self.assertEqual(issued, fresh)
        self.assertEqual(len(calls), 1)

    def test_a_refused_registration_is_reported_as_none(self):
        issued, _calls = self._run([{"isSuccess": 1, "sid": ""}], None)
        self.assertIsNone(issued)


class PasswordHandlingTests(unittest.TestCase):
    def test_the_password_never_reaches_a_log_line(self):
        source = inspect.getsource(auth)
        logged = re.findall(r"_LOGGER\.(?:debug|info|warning|error|exception)\((.*?)\)", source, re.S)
        for call in logged:
            with self.subTest(call=call[:60]):
                self.assertNotIn("password", call.lower())

    def test_the_login_result_carries_the_device_id(self):
        result = auth.HuaweiLoginResult(cookie="a=b", device_id=DEVICE_ID)
        self.assertEqual(result.device_id, DEVICE_ID)
        self.assertIsNone(auth.HuaweiLoginResult(cookie="a=b").device_id)


if __name__ == "__main__":
    unittest.main()

"""Config flow defaults and the native-account flow."""

import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.huawei_find import auth
from custom_components.huawei_find.config_flow import (
    BROWSER_SCHEMA,
    HuaweiFindConfigFlow,
    _scan_interval_value,
)
from custom_components.huawei_find.helpers import reauth_notice_text
from custom_components.huawei_find.const import (
    CONF_DEVICES,
    CONF_DEVICE_ID,
    CONF_DEVICE_ID_BRIDGE,
    CONF_HW_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MAX_SCAN_INTERVAL,
)

DEVICE_ID = "0123456789abcdef" * 5 + "0123"
BRIDGE_ID = "b" * 84


class ScanIntervalValueTests(unittest.TestCase):
    def test_missing_value_uses_default(self):
        self.assertEqual(_scan_interval_value(None), DEFAULT_SCAN_INTERVAL)
        self.assertEqual(_scan_interval_value(""), DEFAULT_SCAN_INTERVAL)

    def test_explicit_zero_stays_disabled(self):
        self.assertEqual(_scan_interval_value(0), 0)
        self.assertEqual(_scan_interval_value("0"), 0)

    def test_values_are_clamped(self):
        self.assertEqual(_scan_interval_value(30), 30)
        self.assertEqual(_scan_interval_value(-10), 0)
        self.assertEqual(_scan_interval_value(10_000), MAX_SCAN_INTERVAL)

    def test_unparsable_value_falls_back_to_default(self):
        self.assertEqual(_scan_interval_value("bad"), DEFAULT_SCAN_INTERVAL)


class AccountFlowTests(unittest.IsolatedAsyncioTestCase):
    def _flow(self) -> HuaweiFindConfigFlow:
        flow = HuaweiFindConfigFlow()
        flow.hass = MagicMock()
        return flow

    def _prepare_success(self, flow: HuaweiFindConfigFlow) -> None:
        flow._validate_cookie = AsyncMock(return_value=([], None))
        flow._async_set_unique_id = AsyncMock()
        flow.async_step_devices = AsyncMock(
            return_value={"type": "form", "step_id": "devices"}
        )

    async def test_user_menu_offers_the_account_path(self):
        result = await self._flow().async_step_user()

        self.assertEqual(result["type"], "menu")
        self.assertIn("account", result["menu_options"])
        self.assertIn("paste", result["menu_options"])

    async def test_reauth_menu_offers_the_account_path_and_loads_identity(self):
        flow = self._flow()

        result = await flow.async_step_reauth(
            {CONF_DEVICE_ID: DEVICE_ID, CONF_DEVICE_ID_BRIDGE: BRIDGE_ID}
        )

        self.assertIn("reauth_account", result["menu_options"])
        self.assertEqual(flow._device_id, DEVICE_ID)
        self.assertEqual(flow._bridge_device_id, BRIDGE_ID)

    async def test_account_login_stores_the_device_id_not_the_password(self):
        flow = self._flow()
        self._prepare_success(flow)
        result_obj = auth.HuaweiLoginResult(
            cookie="USER_ID=42; token=x",
            device_id=DEVICE_ID,
            bridge_device_id=BRIDGE_ID,
        )
        with patch.object(auth, "async_login", AsyncMock(return_value=result_obj)) as login:
            result = await flow.async_step_account(
                {
                    "account": "13800138000",
                    "password": "pw",
                    "login_id": "",
                }
            )

        self.assertEqual(result["step_id"], "devices")
        self.assertEqual(flow._pending_device_id, DEVICE_ID)
        self.assertEqual(flow._pending_bridge_device_id, BRIDGE_ID)
        self.assertEqual(flow._pending_password, "")
        self.assertIsNone(login.await_args.kwargs["device_id"])

    async def test_validate_failure_keeps_the_new_device_id_for_the_retry(self):
        flow = self._flow()
        flow._validate_cookie = AsyncMock(return_value=([], "cannot_connect"))
        flow._async_set_unique_id = AsyncMock()
        flow.async_step_devices = AsyncMock()
        result_obj = auth.HuaweiLoginResult(
            cookie="USER_ID=42; token=x",
            device_id=DEVICE_ID,
            bridge_device_id=BRIDGE_ID,
        )
        with patch.object(auth, "async_login", AsyncMock(return_value=result_obj)):
            result = await flow.async_step_account(
                {"account": "13800138000", "password": "pw", "login_id": ""}
            )

        self.assertEqual(result["errors"]["base"], "cannot_connect")
        self.assertEqual(flow._device_id, DEVICE_ID)
        self.assertEqual(flow._bridge_device_id, BRIDGE_ID)
        self.assertEqual(flow._pending_device_id, DEVICE_ID)
        flow.async_step_devices.assert_not_awaited()

    async def test_browser_options_are_empty_when_no_browser_step_was_used(self):
        self.assertEqual(self._flow()._browser_options(), {})

    async def test_empty_device_selection_is_rejected(self):
        flow = self._flow()
        flow._devices = [{"id": "d1", "name": "Phone", "is_shared": False}]

        result = await flow.async_step_devices(
            {CONF_DEVICES: [], CONF_SCAN_INTERVAL: 15}
        )

        self.assertEqual(result["errors"]["base"], "select_devices")

    async def test_cookie_without_user_id_is_rejected(self):
        from custom_components.huawei_find import config_flow

        class Client:
            user_id = None

            async def async_get_devices(self):
                return [{"id": "d1"}]

            async def async_close(self):
                return None

        flow = self._flow()
        with patch.object(config_flow, "HuaweiFindClient", return_value=Client()):
            devices, error = await flow._validate_cookie("cookie")

        self.assertIsNone(devices)
        self.assertEqual(error, "invalid_auth")

    async def test_account_choice_reuses_the_first_registered_device(self):
        flow = self._flow()
        self._prepare_success(flow)
        first = auth.HuaweiLoginAccountChoice(
            ["111", "222"], device_id=DEVICE_ID, bridge_device_id=BRIDGE_ID
        )
        second = auth.HuaweiLoginResult(
            cookie="USER_ID=42; token=x",
            device_id=DEVICE_ID,
            bridge_device_id=BRIDGE_ID,
        )
        login = AsyncMock(side_effect=[first, second])
        with patch.object(auth, "async_login", login):
            choice = await flow.async_step_account(
                {
                    "account": "13800138000",
                    "password": "pw",
                    "login_id": "",
                }
            )
            result = await flow.async_step_account_choice({"login_id": "111"})

        self.assertEqual(choice["step_id"], "account_choice")
        self.assertEqual(flow._device_id, DEVICE_ID)
        self.assertEqual(result["step_id"], "devices")
        self.assertEqual(login.await_args_list[1].kwargs["device_id"], DEVICE_ID)
        self.assertEqual(login.await_args_list[1].kwargs["bridge_device_id"], BRIDGE_ID)
        self.assertEqual(flow._pending_password, "")

    async def test_device_identity_error_has_its_own_message(self):
        flow = self._flow()
        with patch.object(
            auth,
            "async_login",
            AsyncMock(side_effect=auth.HuaweiLoginDeviceError("rejected")),
        ):
            result = await flow.async_step_account(
                {
                    "account": "13800138000",
                    "password": "pw",
                    "login_id": "",
                }
            )

        self.assertEqual(result["errors"]["base"], "device_identity_error")

    async def test_blank_login_session_has_its_own_message(self):
        flow = self._flow()
        with patch.object(
            auth,
            "async_login",
            AsyncMock(side_effect=auth.HuaweiLoginSessionError("blank")),
        ):
            result = await flow.async_step_account(
                {
                    "account": "13800138000",
                    "password": "pw",
                    "login_id": "",
                }
            )

        self.assertEqual(result["errors"]["base"], "login_session_failed")


    async def test_reauth_account_finishes_the_reauth_flow(self):
        flow = self._flow()
        self._prepare_success(flow)
        flow._finish_reauth = MagicMock(
            return_value={"type": "abort", "reason": "reauth_successful"}
        )
        result_obj = auth.HuaweiLoginResult(
            cookie="USER_ID=42; token=x", device_id=DEVICE_ID
        )
        with patch.object(auth, "async_login", AsyncMock(return_value=result_obj)):
            result = await flow.async_step_reauth_account(
                {
                    "account": "13800138000",
                    "password": "pw",
                    "login_id": "",
                }
            )

        self.assertEqual(result["reason"], "reauth_successful")
        flow._finish_reauth.assert_called_once()
        flow.async_step_devices.assert_not_awaited()


class ReauthOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def _flow_with_entry(self, cookie_user: str) -> tuple[HuaweiFindConfigFlow, MagicMock]:
        flow = HuaweiFindConfigFlow()
        flow.hass = MagicMock()
        entry = MagicMock()
        entry.unique_id = "huawei_42"
        entry.data = {}
        entry.options = {}
        flow.hass.config_entries.async_get_entry.return_value = entry
        flow.hass.config_entries.async_reload = AsyncMock()
        flow.hass.async_create_task = lambda coro: coro.close()
        flow.context = {"entry_id": "entry-1"}
        flow._cookie = f"USER_ID={cookie_user}; token=x"
        return flow, entry

    def test_reauth_refuses_a_cookie_for_another_account(self):
        flow, entry = self._flow_with_entry("41")

        result = flow._finish_reauth()

        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "wrong_account")
        flow.hass.config_entries.async_update_entry.assert_not_called()

    def test_reauth_writes_the_device_id_and_never_the_password(self):
        flow, entry = self._flow_with_entry("42")
        flow._pending_device_id = DEVICE_ID
        flow._pending_bridge_device_id = BRIDGE_ID
        flow._pending_password = "secret"

        result = flow._finish_reauth()

        self.assertEqual(result["reason"], "reauth_successful")
        data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertEqual(data[CONF_DEVICE_ID], DEVICE_ID)
        self.assertEqual(data[CONF_DEVICE_ID_BRIDGE], BRIDGE_ID)
        self.assertNotIn(CONF_HW_PASSWORD, data)

    def test_reauth_keeps_the_existing_browserless_options(self):
        flow, entry = self._flow_with_entry("42")
        entry.options = {
            "browser_url": "http://browser:3000",
            "browser_token": "token",
            "username": "user",
            "password": "pass",
        }
        flow._cookie = "USER_ID=42; token=x"

        result = flow._finish_reauth()

        self.assertEqual(result["reason"], "reauth_successful")
        options = flow.hass.config_entries.async_update_entry.call_args.kwargs["options"]
        self.assertEqual(options["browser_url"], "http://browser:3000")
        self.assertEqual(options["username"], "user")
        self.assertEqual(options["password"], "pass")


class CopyConsistencyTests(unittest.TestCase):
    """Keep the three translation files honest about the account path."""

    def _load(self, relative: str) -> dict:
        root = Path(__file__).resolve().parents[1]
        return json.loads((root / relative).read_text(encoding="utf-8"))

    def test_account_copy_is_consistent_and_explains_errors(self):
        files = (
            "custom_components/huawei_find/strings.json",
            "custom_components/huawei_find/translations/en.json",
            "custom_components/huawei_find/translations/zh-Hans.json",
        )
        for relative in files:
            with self.subTest(file=relative):
                data = self._load(relative)["config"]
                for step in ("account", "reauth_account"):
                    self.assertNotIn("reset_device", data["step"][step]["data"])
                    self.assertNotIn("device_fp", data["step"][step]["data"])
                self.assertIn("login_session_failed", data["error"])
                self.assertIn("device_identity_error", data["error"])
                identity_error = data["error"]["device_identity_error"].lower()
                self.assertNotIn("reset device", identity_error)
                self.assertNotIn("重置", identity_error)
                reauth = data["step"]["reauth_confirm"]["description"].lower()
                self.assertNotIn("both methods", reauth)
                self.assertNotIn("同一页", reauth)

    def test_reauth_menu_has_no_legacy_form_copy(self):
        """The single-page form is gone: keep its 7 data fields out too."""
        files = (
            "custom_components/huawei_find/strings.json",
            "custom_components/huawei_find/translations/en.json",
            "custom_components/huawei_find/translations/zh-Hans.json",
        )
        for relative in files:
            with self.subTest(file=relative):
                step = self._load(relative)["config"]["step"]["reauth_confirm"]
                self.assertIn("menu_options", step)
                self.assertNotIn("data", step)
                self.assertNotIn("data_description", step)


class ReauthMenuOrderTests(unittest.IsolatedAsyncioTestCase):
    """The cheapest path comes first and the legacy form step stays gone."""

    def _flow(self) -> HuaweiFindConfigFlow:
        flow = HuaweiFindConfigFlow()
        flow.hass = MagicMock()
        return flow

    async def test_reauth_menu_puts_the_account_path_first(self):
        flow = self._flow()
        entry = MagicMock()
        entry.unique_id = "huawei_30086000615965581"
        flow.hass.config_entries.async_get_entry.return_value = entry

        result = await flow.async_step_reauth({CONF_DEVICE_ID: DEVICE_ID})

        self.assertEqual(result["type"], "menu")
        self.assertEqual(result["step_id"], "reauth_confirm")
        self.assertEqual(
            result["menu_options"],
            ["reauth_account", "reauth_paste", "reauth_browser"],
        )
        self.assertEqual(result["description_placeholders"]["account"], "5581")

    async def test_user_menu_puts_the_account_path_first(self):
        flow = self._flow()

        result = await flow.async_step_user()

        self.assertEqual(result["menu_options"], ["account", "paste", "browser"])

    async def test_reauth_menu_survives_a_redraw_without_entry_data(self):
        """HA redraws a menu by calling the step again with None; that must work."""
        flow = self._flow()

        first = await flow.async_step_reauth(None)
        redraw = await flow.async_step_reauth_confirm(None)

        for result in (first, redraw):
            with self.subTest(result=result["step_id"]):
                self.assertEqual(result["type"], "menu")
                self.assertEqual(result["step_id"], "reauth_confirm")

    async def test_every_menu_step_id_has_a_handler_method(self):
        """Home Assistant raises UnknownStep unless async_step_<step_id> exists.

        A menu result is checked the same way as a form, so a menu whose
        step_id has no method aborts the flow before any repair issue is made.
        """
        flow = self._flow()
        entry = MagicMock()
        entry.unique_id = "huawei_30086000615965581"
        flow.hass.config_entries.async_get_entry.return_value = entry

        for result in (
            await flow.async_step_user(),
            await flow.async_step_reauth({CONF_DEVICE_ID: DEVICE_ID}),
        ):
            with self.subTest(step_id=result["step_id"]):
                self.assertTrue(
                    hasattr(flow, f"async_step_{result['step_id']}"),
                    f"no async_step_{result['step_id']} for the menu",
                )


class BrowserSchemaDefaultsTests(unittest.TestCase):
    def test_credentials_are_not_stored_by_default(self):
        validated = BROWSER_SCHEMA(
            {
                "browser_url": "http://browser:3000",
                "username": "user",
                "password": "pass",
            }
        )

        self.assertIs(validated["store_credentials"], False)


class ReauthNoticeTests(unittest.TestCase):
    def _hass(self, language: str):
        hass = MagicMock()
        hass.config.language = language
        return hass

    def test_chinese_notice_lists_all_three_paths(self):
        title, message = reauth_notice_text(
            self._hass("zh-Hans"), "expired", "", False
        )

        self.assertIn("重新授权", title)
        self.assertIn("华为账号密码", message)
        self.assertIn("粘贴新 Cookie", message)
        self.assertIn("browserless", message)
        self.assertIn("未配置自动重登", message)

    def test_english_notice_follows_the_language_and_reports_the_error(self):
        title, message = reauth_notice_text(self._hass("en"), "expired", "boom", True)

        self.assertIn("re-authentication", title.lower())
        self.assertIn("Automatic re-login failed: boom", message)
        self.assertIn("cloud.huawei.com", message)


if __name__ == "__main__":
    unittest.main()

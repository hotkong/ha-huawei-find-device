"""Optional browser-login fallback.

Cookie paste stays the primary authentication. When a configured session is
rejected and the user has provided a browserless-compatible endpoint plus
Huawei credentials, this module logs in with a headless browser and returns a
fresh cookie header. It never logs the password or the cookie value.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import HuaweiFindError, cookie_is_usable, cookie_matches_entry
from .helpers import abort_reauth_flows
from .const import (
    AUTO_LOGIN_COOLDOWN,
    BROWSER_LOGIN_TIMEOUT,
    CONF_AUTH_TIME,
    CONF_COOKIE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

LOGIN_CODE = r"""
export default async ({ page }) => {
  const USER = __USERNAME__;
  const PASSWORD = __PASSWORD__;
  const out = { ok: false, url: "", title: "", error: "", needsVerify: false, cookieHeader: "" };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  try {
    await page.setViewport({ width: 1280, height: 900 });
    await page.goto("https://cloud.huawei.com/", { waitUntil: "domcontentloaded", timeout: 25000 });

    let frame = null;
    for (let i = 0; i < 10 && !frame; i++) {
      await sleep(1000);
      frame = page.frames().find((f) => f.url().includes("id1.cloud.huawei.com"));
    }
    if (!frame) {
      out.error = "no_login_frame";
      out.url = page.url();
      return out;
    }

    const userInput = await frame.waitForSelector(
      'input[placeholder="Phone/Email/Login ID"], input[placeholder*="Phone"], input[name="username"]',
      { visible: true, timeout: 9000 }
    );
    await userInput.click({ clickCount: 3 });
    await userInput.type(USER, { delay: 30 });

    const pwdInput = await frame.waitForSelector(
      'input[placeholder="Password"], input[placeholder*="密码"], input[type="password"]',
      { visible: true, timeout: 6000 }
    );
    await pwdInput.click({ clickCount: 3 });
    await pwdInput.type(PASSWORD, { delay: 30 });

    try {
      const boxes = await frame.$$("input[type=checkbox]");
      for (const box of boxes) {
        const checked = await box.evaluate((el) => el.checked);
        if (!checked) await box.click().catch(() => {});
      }
    } catch (e) {}

    let clicked = false;
    for (const sel of ["div.hwid-login-btn", ".hwid-btn.hwid-login-btn", "[ht=click_pwdlogin_submitLogin]"]) {
      const btn = await frame.$(sel);
      if (btn) { await btn.click(); clicked = true; break; }
    }
    if (!clicked) await pwdInput.press("Enter");

    const deadline = Date.now() + 35000;
    while (Date.now() < deadline) {
      const cookies = await page.cookies();
      if (cookies.some((c) => c.name === "USER_ID" || c.name === "userId")) break;
      await sleep(700);
    }
    await sleep(2000);

    out.url = page.url();
    out.title = await page.title();
    try {
      const frameText = (await frame.evaluate(() => document.body && document.body.innerText)) || "";
      out.needsVerify = /验证码|安全验证|身份验证|请完成验证|verify code/i.test(String(frameText));
    } catch (e) {}

    const cookies = await page.cookies();
    if (cookies.some((c) => c.name === "USER_ID" || c.name === "userId")) {
      const header = cookies.map((c) => c.name + "=" + c.value).join("; ");
      if (header.length > 20) {
        out.ok = true;
        out.cookieHeader = header;
      }
    }
    if (!out.ok && !out.error) out.error = out.needsVerify ? "verification_required" : "login_incomplete";
    return out;
  } catch (e) {
    out.error = String(e).slice(0, 300);
    return out;
  }
};
"""


class BrowserLoginError(HuaweiFindError):
    """Browser login failed (configuration, network, verification, ...)."""


def build_login_code(username: str, password: str) -> str:
    """Render the browserless function with safely quoted credentials."""
    return LOGIN_CODE.replace("__USERNAME__", json.dumps(username)).replace(
        "__PASSWORD__", json.dumps(password)
    )


def cooldown_remaining(last_attempt: float, now: float | None = None) -> float:
    """Seconds until the next automatic attempt is allowed.

    ``0.0`` means "never attempted". On a freshly started HA process the
    monotonic clock is still small, so subtracting it from zero would otherwise
    look like an active cooldown and skip the first automatic re-login.
    """
    if last_attempt <= 0:
        return 0.0
    now = time.monotonic() if now is None else now
    return max(0.0, AUTO_LOGIN_COOLDOWN - (now - last_attempt))


async def async_browser_login(
    browser_url: str, username: str, password: str, token: str = ""
) -> str:
    """Run one browserless /function login and return the cookie header."""
    url = browser_url.rstrip("/") + f"/function?timeout={BROWSER_LOGIN_TIMEOUT * 1000}"
    if token:
        url += f"&token={quote(token)}"

    timeout = aiohttp.ClientTimeout(total=BROWSER_LOGIN_TIMEOUT + 20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, json={"code": build_login_code(username, password)}
            ) as response:
                if response.status != 200:
                    raise BrowserLoginError(
                        f"browserless returned HTTP {response.status}"
                    )
                data: dict[str, Any] = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        raise BrowserLoginError(f"cannot reach browserless: {err}") from err

    if not isinstance(data, dict):
        raise BrowserLoginError("browserless returned an unexpected response")

    cookie = str(data.get("cookieHeader") or "")
    if not data.get("ok") or not cookie_is_usable(cookie):
        reason = data.get("error") or "no usable cookie"
        if data.get("needsVerify"):
            reason = f"verification required ({reason})"
        raise BrowserLoginError(str(reason))
    return cookie


async def async_try_browser_login(
    hass: HomeAssistant,
    runtime: Any,
    reason: str = "",
    force: bool = False,
) -> bool:
    """Try the configured browser fallback and persist a fresh cookie."""
    if not (runtime.browser_url and runtime.browser_username and runtime.browser_password):
        return False
    if runtime.login_lock.locked():
        return False
    if not force:
        remaining = cooldown_remaining(runtime.last_login_attempt)
        if remaining > 0:
            _LOGGER.debug(
                "Browser login skipped (cooldown %.0fs left), reason=%s", remaining, reason
            )
            return False

    async with runtime.login_lock:
        runtime.last_login_attempt = time.monotonic()
        _LOGGER.info("Browser login attempt for %s (reason=%s)", runtime.entry.title, reason or "manual")
        try:
            cookie = await async_browser_login(
                runtime.browser_url,
                runtime.browser_username,
                runtime.browser_password,
                runtime.browser_token,
            )
        except BrowserLoginError as err:
            runtime.login_error = str(err)
            _LOGGER.warning("Browser login failed for %s: %s", runtime.entry.title, err)
            return False

        if not cookie_matches_entry(cookie, runtime.entry.unique_id):
            runtime.login_error = "browser login returned a different Huawei account"
            _LOGGER.warning(
                "Browser login returned another account for %s", runtime.entry.title
            )
            return False

        try:
            runtime.client.set_cookie(cookie)
        except HuaweiFindError as err:
            runtime.login_error = f"rejected cookie: {err}"
            _LOGGER.warning("Browser login produced a rejected cookie: %s", err)
            return False

        hass.config_entries.async_update_entry(
            runtime.entry,
            data={
                **runtime.entry.data,
                CONF_COOKIE: cookie,
                CONF_AUTH_TIME: dt_util.utcnow().isoformat(),
            },
        )
        runtime.reauth_requested = False
        runtime.expiry_warned = False
        runtime.login_error = None
        runtime.last_login_success = dt_util.utcnow()
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_reauth_{runtime.entry.entry_id}"
        )
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_expiry_{runtime.entry.entry_id}"
        )
        abort_reauth_flows(hass, runtime.entry)
        _LOGGER.info("Browser login succeeded for %s", runtime.entry.title)
        return True

"""Automatic re-login orchestration for the optional browser fallback."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .browser_login import async_try_browser_login, cooldown_remaining

_LOGGER = logging.getLogger(__name__)

RECOVERED = "recovered"
FAILED = "failed"
SKIPPED = "skipped"


async def async_try_auto_login(
    hass: HomeAssistant,
    runtime: Any,
    reason: str = "",
    force: bool = False,
) -> str:
    """Try the browser fallback and classify the outcome.

    Returns RECOVERED, FAILED (safe to prompt the user) or SKIPPED (another
    attempt is running or the cooldown has not elapsed; retry later instead of
    telling the user to paste a cookie again).
    """
    if not (runtime.browser_url and runtime.browser_username and runtime.browser_password):
        return FAILED

    if runtime.login_lock.locked():
        return SKIPPED
    if not force and cooldown_remaining(runtime.last_login_attempt) > 0:
        # A previous attempt failed -> the user may act now; a previous success
        # means the session should still be fresh, so do not prompt yet.
        return FAILED if runtime.login_error else SKIPPED

    if await async_try_browser_login(hass, runtime, reason, force=force):
        return RECOVERED
    return FAILED

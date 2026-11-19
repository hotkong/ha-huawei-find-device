"""HTTP client for the Huawei Cloud find-device API.

The web portal (cloud.huawei.com/webFindPhone.html) uses these endpoints:
    POST /findDevice/getMobileDeviceList   {"deviceType": 0}
    POST /findDevice/getShareGrantInfo     {"traceId": ...}
    POST /findDevice/locate                {device, traceId, ...}
    POST /findDevice/queryLocateResult     {device, sequence, ...}

Authentication is the browser session cookie (must contain CSRFToken and
USER_ID), so no browser and no Huawei account password are required.
"""


from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

import aiohttp

from .const import (
    BATTERY_STALE_SECONDS,
    EP_BELL,
    HW_MODEL,
    BASE_URL,
    EP_DEVICE_LIST,
    EP_LOCATE,
    EP_HEARTBEAT,
    EP_QUERY,
    EP_SHARE_GRANTS,
    LOCATE_TIMEOUT,
    POLL_INTERVAL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class HuaweiFindError(Exception):
    """Generic error (network or API failure)."""


class HuaweiFindAuthError(HuaweiFindError):
    """The session is no longer valid; a new cookie is required."""


class HuaweiFindDeviceError(HuaweiFindError):
    """Device-level failure (offline, inactive); retrying is pointless."""


# ---------------------------------------------------------------------------
# Cookie / request helpers
# ---------------------------------------------------------------------------


def trace_id(module: str = "01100") -> str:
    """Trace id in the format the web portal uses."""
    return f"{module}_02_{int(time.time())}_{random.randint(0, 99999999):08d}"


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a browser Cookie header string into a dict."""
    cookies: dict[str, str] = {}
    for part in raw.replace("\r", ";").replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            cookies[key] = value.strip()
    return cookies


def _cookie_value(cookies: dict[str, str], names: tuple[str, ...]) -> str:
    lowered = {key.lower(): value for key, value in cookies.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Coordinate conversion (Huawei returns GCJ-02 / BD-09, HA needs WGS-84)
# ---------------------------------------------------------------------------

_A = 6378245.0
_EE = 0.00669342162296594323
_X_PI = math.pi * 3000.0 / 180.0


def _out_of_china(lng: float, lat: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """GCJ-02 -> WGS-84, returns (lng, lat)."""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lng - dlng, lat - dlat


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """WGS-84 -> GCJ-02 (Chinese map services), returns (lng, lat)."""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lng + dlng, lat + dlat


def bd09_to_gcj02(bd_lng: float, bd_lat: float) -> tuple[float, float]:
    """BD-09 -> GCJ-02, returns (lng, lat)."""
    x = bd_lng - 0.0065
    y = bd_lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.cos(theta), z * math.sin(theta)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _coords_to_wgs84(candidate: dict[str, Any]) -> tuple[float | None, float | None, str]:
    """Convert one coordinate dict (sysType 1=WGS-84, 2=BD-09, else GCJ-02)."""
    lat = _as_float(candidate.get("latitude"))
    lng = _as_float(candidate.get("longitude"))
    if lat is None or lng is None:
        return None, None, ""
    sys_type = str(candidate.get("sysType", ""))
    if sys_type == "1":
        return lng, lat, "wgs84"
    if sys_type == "2":
        glng, glat = bd09_to_gcj02(lng, lat)
        wlng, wlat = gcj02_to_wgs84(glng, glat)
        return wlng, wlat, "bd09"
    wlng, wlat = gcj02_to_wgs84(lng, lat)
    return wlng, wlat, "gcj02"


COMPONENT_MAIN = "main"
COMPONENT_CASE = "case"
COMPONENT_LEFT = "left"
COMPONENT_RIGHT = "right"

# Earbuds report one cptLocateInfoList entry per part; the portal itself labels
# them "充电盒 FID / 左耳机 FID / 右耳机 FID" (findPhoneTws0/1/2).
_EARBUD_COMPONENTS = {
    "0": COMPONENT_CASE,
    "1": COMPONENT_LEFT,
    "2": COMPONENT_RIGHT,
}


def component_name(cpt_list: Any, per_device_type: Any = None) -> str:
    """Map one cptLocateInfoList entry to a stable component name.

    Only earbuds (per_device_type "1") report several parts. Phones have no
    components; watches/bands report a single "0" entry that is the whole
    device. Shared devices are phones and are treated as "main" as well.
    """
    if str(per_device_type or "") == "1":
        key = str(cpt_list or "")
        return _EARBUD_COMPONENTS.get(key, f"component_{key or 'unknown'}")
    return COMPONENT_MAIN


def components_from_locate_result(
    locate_result: Any, per_device_type: Any = None
) -> list[str]:
    """Component names a device has already reported (used to build entities)."""
    if not isinstance(locate_result, dict):
        return []
    names: list[str] = []
    for entry in locate_result.get("cptLocateInfoList") or []:
        name = component_name(_json_dict(entry).get("cptList"), per_device_type)
        if name not in names:
            names.append(name)
    return names


def aggregate_battery(batteries: dict[str, Any] | None) -> int | None:
    """Lowest reported percentage (the value a low-battery automation needs)."""
    values = [
        item.get("percentage")
        for item in (batteries or {}).values()
        if isinstance(item, dict) and item.get("percentage") is not None
    ]
    return min(values) if values else None


def _epoch_seconds(timestamp: Any) -> float | None:
    """Accept epoch seconds or milliseconds and return seconds."""
    value = _as_float(timestamp)
    if value is None or value <= 0:
        return None
    if value > 1e11:  # milliseconds since epoch (what Huawei sends)
        value /= 1000
    return value


def _timestamp_iso(timestamp: Any) -> str | None:
    seconds = _epoch_seconds(timestamp)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _normalize_timestamp(value: Any) -> str | None:
    """ISO-8601 for epoch timestamps; otherwise keep the vendor string as-is."""
    if value in (None, ""):
        return None
    return _timestamp_iso(value) or str(value)


def _component_battery(
    status: dict[str, Any],
    reported_at: Any,
    connect_type: Any,
    result_stale: bool,
) -> dict[str, Any]:
    """One component's battery reading plus how trustworthy it is."""
    percentage = _as_float(status.get("percentage"))
    if percentage is not None and not 0 <= percentage <= 100:
        percentage = None
    charging = status.get("isCharging")
    seconds = _epoch_seconds(reported_at)
    stale = bool(result_stale)
    if seconds is not None:
        stale = stale or (time.time() - seconds) > BATTERY_STALE_SECONDS
    return {
        "percentage": int(percentage) if percentage is not None else None,
        "charging": str(charging) == "1" if charging not in (None, "") else None,
        "reported_at": _timestamp_iso(reported_at),
        "connect_type": connect_type,
        "stale": stale,
    }


def _parse_locate_info(
    payload: dict[str, Any], per_device_type: Any = None
) -> dict[str, Any] | None:
    """Extract WGS-84 coordinates, per-component batteries and accuracy."""
    locate_info = _json_dict(payload.get("locateInfo"))
    coord = _json_dict(locate_info.get("coordinateInfo"))

    lat: float | None = None
    lng: float | None = None
    coord_sys = ""
    accuracy: Any = None
    device_time: Any = None
    battery_status = _json_dict(locate_info.get("batteryStatus"))

    top_lat_wgs = _as_float(locate_info.get("latitude_WGS"))
    top_lng_wgs = _as_float(locate_info.get("longitude_WGS"))
    coord_lat = _as_float(coord.get("latitude"))
    coord_lng = _as_float(coord.get("longitude"))
    top_lat = _as_float(locate_info.get("latitude"))
    top_lng = _as_float(locate_info.get("longitude"))

    if top_lat_wgs is not None and top_lng_wgs is not None:
        lng, lat, coord_sys = top_lng_wgs, top_lat_wgs, "wgs84"
    elif coord_lat is not None and coord_lng is not None:
        lng, lat, coord_sys = _coords_to_wgs84(coord)
    elif top_lat is not None and top_lng is not None:
        # Verified against latitude_WGS: the top-level fields are BD-09.
        glng, glat = bd09_to_gcj02(top_lng, top_lat)
        lng, lat = gcj02_to_wgs84(glng, glat)
        coord_sys = "bd09"

    if lat is None or lng is None:
        # Watches/bands/earbuds return the result in cptLocateInfoList instead.
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for entry in payload.get("cptLocateInfoList") or []:
            entry_dict = _json_dict(entry)
            for raw in entry_dict.get("coordinateInfoList") or []:
                candidate = _json_dict(raw)
                if _as_float(candidate.get("latitude")) is not None:
                    candidates.append((candidate, entry_dict))
        chosen = next(
            ((c, e) for c, e in candidates if str(c.get("sysType")) == "1"),
            candidates[0] if candidates else None,
        )
        if chosen is not None:
            candidate, entry_dict = chosen
            lng, lat, coord_sys = _coords_to_wgs84(candidate)
            accuracy = candidate.get("accuracy")
            device_time = candidate.get("time")

    if lat is None or lng is None:
        return None

    if accuracy is None:
        accuracy = coord.get("accuracy")
    if accuracy is None:
        accuracy = locate_info.get("accuracy")
    if device_time is None:
        device_time = (
            coord.get("time")
            or coord.get("currentTime")
            or locate_info.get("executeTime")
            or locate_info.get("locateTime")
            or locate_info.get("updateTime")
        )

    # --- batteries: phones report one value, wearables/earbuds per component.
    execute_time = payload.get("executeTime") or locate_info.get("executeTime")
    exe_result = str(payload.get("exeResult") or "")
    # "900" means the cloud is replaying a cached result (device offline).
    result_stale = exe_result not in ("", "0")

    batteries: dict[str, dict[str, Any]] = {}
    for entry in payload.get("cptLocateInfoList") or []:
        entry_dict = _json_dict(entry)
        name = component_name(entry_dict.get("cptList"), per_device_type)
        if name in batteries:
            continue
        reported_at = execute_time
        for raw in entry_dict.get("coordinateInfoList") or []:
            stamp = _json_dict(raw).get("time")
            if _epoch_seconds(stamp) is not None:
                reported_at = stamp
                break
        batteries[name] = _component_battery(
            _json_dict(entry_dict.get("batteryStatus")),
            reported_at,
            entry_dict.get("connectType"),
            result_stale,
        )
    if not batteries:
        batteries[COMPONENT_MAIN] = _component_battery(
            battery_status, execute_time, None, result_stale
        )

    battery_f = _as_float(locate_info.get("battery"))
    if not battery_f:
        battery_f = _as_float(aggregate_battery(batteries))
    charging = battery_status.get("isCharging")
    charging = str(charging) == "1" if charging not in (None, "") else None

    return {
        "latitude": round(lat, 6),
        "longitude": round(lng, 6),
        "accuracy": _as_float(accuracy),
        "battery": int(battery_f) if battery_f is not None else None,
        "batteries": batteries,
        "battery_stale": bool(
            result_stale or any(item["stale"] for item in batteries.values())
        ),
        "battery_charging": charging,
        "result_stale": result_stale,
        "result_time": _timestamp_iso(execute_time),
        "screen_locked": str(locate_info.get("isLockScreen")) == "1"
        if locate_info.get("isLockScreen") is not None
        else None,
        "coordinate_system": coord_sys,
        "device_time": _normalize_timestamp(device_time),
        "located_at": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class HuaweiFindClient:
    """Pure HTTP client for the Huawei Cloud find-device API."""

    def __init__(
        self, cookie: str, cookie_listener: Callable[[dict[str, str]], None] | None = None
    ) -> None:
        self._cookie_listener = cookie_listener
        self._cookies = parse_cookie(cookie)
        if not self._cookies:
            raise HuaweiFindError("Cookie is empty")
        self.csrf_token = _cookie_value(
            self._cookies, ("CSRFToken", "hicloud_csrf", "csrfToken", "XSRF-TOKEN", "_csrf")
        )
        self.user_id = _cookie_value(self._cookies, ("USER_ID", "userId", "uid"))
        self._session: aiohttp.ClientSession | None = None

    def set_cookie(self, cookie: str) -> None:
        """Replace the session cookies (used by huawei_find.set_cookie)."""
        cookies = parse_cookie(cookie)
        if not cookies:
            raise HuaweiFindError("Cookie is empty")
        self._cookies = cookies
        self.csrf_token = _cookie_value(
            self._cookies,
            ("CSRFToken", "hicloud_csrf", "csrfToken", "XSRF-TOKEN", "_csrf"),
        )
        self.user_id = _cookie_value(self._cookies, ("USER_ID", "userId", "uid"))

    async def async_close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/webFindPhone.html",
            "User-Agent": USER_AGENT,
            "x-hw-framework-type": "web",
            "x-hw-need-location": "1",
            "x-hw-trace-id": trace_id(),
        }
        if payload is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        if self.csrf_token:
            headers["CSRFToken"] = self.csrf_token
        if self.user_id:
            headers["userId"] = self.user_id
        if extra_headers:
            headers.update(extra_headers)

        session = await self._ensure_session()
        try:
            async with session.request(
                method,
                f"{BASE_URL}{path}",
                json=payload,
                params=params,
                headers=headers,
                cookies=self._cookies,
            ) as response:
                status = response.status
                text = await response.text()
                self._absorb_set_cookie(response)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HuaweiFindError(f"Request to Huawei Cloud failed: {err}") from err

        if status in (401, 403):
            raise HuaweiFindAuthError(
                f"Huawei Cloud returned HTTP {status}; the session may have expired"
            )
        if status in (408, 425, 429) or status >= 500:
            # Gateway/CDN/rate-limit errors are transient, not an expired session
            raise HuaweiFindError(
                f"Huawei Cloud returned HTTP {status}; temporary outage"
            )
        if not text.lstrip().startswith("{"):
            raise HuaweiFindAuthError(
                "Huawei Cloud returned non-JSON content (usually means the session expired)"
            )

        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise HuaweiFindError(
                f"Unable to parse Huawei Cloud response: {text[:120]}"
            ) from err

        try:
            code = int(data.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        data["_code"] = code
        return data

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, payload=payload)

    async def async_heartbeat(self) -> bool:
        """Keep the web session alive (the portal calls this while open)."""
        data = await self._request(
            "GET",
            EP_HEARTBEAT,
            params={"checkType": 1, "traceId": trace_id()},
        )
        if data["_code"] == 990:
            raise HuaweiFindAuthError(
                str(data.get("info") or "authentication failed (code 990)")
            )
        return True

    def _absorb_set_cookie(self, response: aiohttp.ClientResponse) -> None:
        """Huawei Cloud rotates JSESSIONID/token; persist them or the next call fails."""
        changed = False
        for raw in response.headers.getall("Set-Cookie", []):
            name, _, rest = raw.partition("=")
            name = name.strip()
            value = rest.split(";", 1)[0].strip()
            if not name:
                continue
            if not value:
                # Deletion: Set-Cookie: name=; Max-Age=0 / Expires in the past
                lowered = rest.lower()
                if ("max-age=0" in lowered or "1970" in lowered) and name in self._cookies:
                    del self._cookies[name]
                    changed = True
                continue
            if self._cookies.get(name) != value:
                self._cookies[name] = value
                changed = True
        if not changed:
            return
        self.csrf_token = _cookie_value(
            self._cookies, ("CSRFToken", "hicloud_csrf", "csrfToken", "XSRF-TOKEN", "_csrf")
        )
        self.user_id = _cookie_value(self._cookies, ("USER_ID", "userId", "uid"))
        _LOGGER.debug("Huawei Cloud session cookies updated (%d entries)", len(self._cookies))
        if self._cookie_listener is not None:
            try:
                self._cookie_listener(dict(self._cookies))
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to persist cookies")

    async def async_keepalive(self) -> bool:
        """Lightweight keepalive: call the device list endpoint."""
        data = await self._post(EP_DEVICE_LIST, {"deviceType": 0})
        if data.get("_code") == 990:
            raise HuaweiFindAuthError(str(data.get("info") or "authentication failed (code 990)"))
        return True

    async def _session_is_valid(self) -> bool:
        """Probe the session via the device list endpoint."""
        try:
            data = await self._post(EP_DEVICE_LIST, {"deviceType": 0})
        except HuaweiFindAuthError:
            return False
        except HuaweiFindError:
            return True  # network errors do not imply an expired session
        return str(data.get("_code")) != "990"

    # -- Devices -------------------------------------------------------------

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Return own and shared family devices (deduplicated)."""
        data = await self._post(
            EP_DEVICE_LIST,
            {
                "traceId": trace_id(),
                "tabLocation": 2,
                "portalType": 0,
                "deviceType": 0,
            },
        )
        if data["_code"] != 0:
            # Fall back to the minimal body that older builds accepted
            fallback = await self._post(EP_DEVICE_LIST, {"deviceType": 0})
            if fallback["_code"] == 0:
                data = fallback
        if data["_code"] == 990:
            raise HuaweiFindAuthError(str(data.get("info") or "authentication failed (code 990)"))
        if data["_code"] != 0:
            raise HuaweiFindError(f"Failed to fetch device list: {data.get('info') or data['_code']}")

        devices: list[dict[str, Any]] = []
        for raw in data.get("deviceList") or []:
            device_id = raw.get("deviceId")
            if not device_id:
                continue
            devices.append(
                {
                    "id": device_id,
                    "name": raw.get("deviceAliasName")
                    or raw.get("deviceName")
                    or raw.get("model")
                    or "Huawei device",
                    "model": raw.get("model") or raw.get("deviceTypeName") or "",
                    "device_type": raw.get("deviceType") or 9,
                    "per_device_type": str(raw.get("perDeviceType") or ""),
                    "components": components_from_locate_result(
                        raw.get("locateResult"), raw.get("perDeviceType")
                    ),
                    "capability": list(raw.get("capability") or []),
                    "is_shared": False,
                    "sender_user_id": "",
                }
            )

        try:
            shared = await self._post(EP_SHARE_GRANTS, {"traceId": trace_id()})
        except HuaweiFindAuthError:
            raise
        except HuaweiFindError as err:
            _LOGGER.debug("Failed to fetch shared devices (ignored): %s", err)
            shared = {}

        if shared and shared.get("_code") == 0:
            for grant in shared.get("shareGrantInfoList") or []:
                device_id = grant.get("senderDeviceId")
                sender = grant.get("senderUserId")
                if not device_id or not sender:
                    continue
                devices.append(
                    {
                        "id": device_id,
                        "name": grant.get("senderDeviceName") or "Family device",
                        "model": grant.get("senderDeviceModel") or "",
                        "device_type": grant.get("senderDeviceType") or 9,
                        "is_shared": True,
                        "sender_user_id": sender,
                    }
                )

        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for device in devices:
            key = device["id"]
            if key in seen:
                continue
            seen.add(key)
            unique.append(device)
        return unique

    # -- Locating ------------------------------------------------------------

    def _locate_payload(self, device: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "deviceType": device.get("device_type") or 9,
            "deviceId": device["id"],
            "perDeviceType": str(device.get("per_device_type") or "0"),
            "cptList": "",
            "traceId": trace_id(),
        }
        if device.get("is_shared"):
            payload["senderUserId"] = device["sender_user_id"]
            payload["relationType"] = 2
        return payload

    def _bell_payload(self, device: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "traceId": trace_id(),
            "deviceId": device["id"],
            "deviceType": device.get("device_type") or 9,
            "perDeviceType": str(device.get("per_device_type") or "0"),
            "cptList": "",
        }
        if device.get("is_shared"):
            payload["senderUserId"] = device["sender_user_id"]
            payload["relationType"] = 2
        return payload

    def _query_payload(self, device: dict[str, Any], sequence: int) -> dict[str, Any]:
        payload = {
            "traceId": trace_id(),
            "deviceId": device["id"],
            "deviceType": device.get("device_type") or 9,
            "perDeviceType": str(device.get("per_device_type") or "0"),
            "sequence": sequence,
            "presetDevice": False,
        }
        if device.get("is_shared"):
            payload["senderUserId"] = device["sender_user_id"]
            payload["relationType"] = 2
        return payload

    async def async_locate(self, device: dict[str, Any]) -> bool:
        """Send one locate command (wakes the find-device service on the phone)."""
        data = await self._post(EP_LOCATE, self._locate_payload(device))
        code = str(data.get("_code"))
        if code == "0":
            return True
        info = str(data.get("info") or f"code={code}")
        if code == "990":
            if not await self._session_is_valid():
                raise HuaweiFindAuthError(info)
            raise HuaweiFindDeviceError(
                f"Device is not locatable right now (possibly offline): {info}"
            )
        raise HuaweiFindError(f"Locate command failed: {info}")

    async def async_ring(self, device: dict[str, Any]) -> bool:
        """Make the device play its alarm sound."""
        data = await self._request(
            "POST",
            EP_BELL,
            payload=self._bell_payload(device),
            extra_headers={
                "x-hw-framework-type": "0",
                "x-hw-model": HW_MODEL,
            },
        )
        code = data["_code"]
        if code == 0:
            return True
        info = str(data.get("info") or f"code={code}")
        if code == 990:
            if not await self._session_is_valid():
                raise HuaweiFindAuthError(info)
            raise HuaweiFindDeviceError(f"Device cannot ring right now: {info}")
        raise HuaweiFindError(f"Ring failed: {info}")

    async def async_query(self, device: dict[str, Any], sequence: int = 0) -> dict[str, Any]:
        return await self._post(EP_QUERY, self._query_payload(device, sequence))

    async def async_locate_and_wait(self, device: dict[str, Any]) -> dict[str, Any]:
        """Send locate and poll for the result, up to LOCATE_TIMEOUT seconds."""
        await self.async_locate(device)

        deadline = time.monotonic() + LOCATE_TIMEOUT
        sequence = 0
        last_error = "Timed out waiting for coordinates"

        while time.monotonic() < deadline:
            data = await self.async_query(device, sequence)
            code = data.get("_code", -1)
            if code in (0, 1):
                parsed = _parse_locate_info(data, device.get("per_device_type"))
                if parsed is not None:
                    parsed["status"] = "ok"
                    return parsed
                exe_result = str(
                    data.get("exeResult")
                    or _json_dict(data.get("locateInfo")).get("exeResult")
                    or ""
                )
                if exe_result == "0":
                    last_error = "Huawei Cloud ran the locate but returned no coordinates (device may be offline)"
                    break
            elif code == 990:
                if not await self._session_is_valid():
                    raise HuaweiFindAuthError(str(data.get("info") or "authentication failed (code 990)"))
                last_error = "Device is offline; no coordinates returned"
                break
            elif code == -1:
                last_error = str(data.get("info") or "Network error")
                break
            else:
                last_error = f"API returned code={code}: {data.get('info') or ''}"
                break

            sequence += 1
            await asyncio.sleep(POLL_INTERVAL)

        return {"status": "error", "last_error": last_error}

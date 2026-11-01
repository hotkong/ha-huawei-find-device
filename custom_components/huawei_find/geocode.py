"""Optional reverse geocoding of WGS-84 coordinates.

The Huawei Cloud find-device API only returns coordinates, so a readable
address has to come from a third-party service. Two providers are supported
and can be selected in the integration options:

    - amap (AMap / Gaode web service, requires a key, best CN coverage)
    - osm  (OpenStreetMap Nominatim, no key, coarser CN coverage)

Results are cached by rounded coordinates and OSM requests are serialised to
respect the Nominatim usage policy (max 1 request per second).
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    GEOCODE_AMAP,
    GEOCODE_CACHE_TTL,
    GEOCODE_OSM,
    OSM_MIN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

AMAP_URL = "https://restapi.amap.com/v3/geocode/regeo"
OSM_URL = "https://nominatim.openstreetmap.org/reverse"
_TIMEOUT = aiohttp.ClientTimeout(total=10)

_OSM_LOCK = asyncio.Lock()
_osm_last_request = 0.0
_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_CACHE_MAX_ENTRIES = 256


async def async_reverse_geocode(
    hass: HomeAssistant,
    provider: str,
    api_key: str,
    latitude: float,
    longitude: float,
) -> str | None:
    """Return a readable address, or None on failure (never blocks locating)."""
    if provider == GEOCODE_AMAP and not api_key:
        return None
    if provider not in (GEOCODE_AMAP, GEOCODE_OSM):
        return None

    # ~110 m precision is enough for an address and dramatically reduces calls
    cache_key = (provider, f"{latitude:.3f},{longitude:.3f}")
    cached = _CACHE.get(cache_key)
    if cached is not None and time.monotonic() - cached[1] < GEOCODE_CACHE_TTL:
        return cached[0]

    if provider == GEOCODE_AMAP:
        address = await _amap(hass, api_key, latitude, longitude)
    else:
        address = await _osm(hass, latitude, longitude)

    if address:
        _prune_cache()
        _CACHE[cache_key] = (address, time.monotonic())
    return address


def _prune_cache() -> None:
    if len(_CACHE) < _CACHE_MAX_ENTRIES:
        return
    now = time.monotonic()
    for key in [k for k, (_, ts) in _CACHE.items() if now - ts >= GEOCODE_CACHE_TTL]:
        _CACHE.pop(key, None)


async def _amap(
    hass: HomeAssistant, api_key: str, latitude: float, longitude: float
) -> str | None:
    session = async_get_clientsession(hass)
    params = {
        "key": api_key,
        "location": f"{longitude:.6f},{latitude:.6f}",  # AMap expects lng,lat
        "extensions": "base",
        "radius": 200,
    }
    try:
        async with session.get(AMAP_URL, params=params, timeout=_TIMEOUT) as response:
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("AMap reverse geocode request failed: %s", err)
        return None

    if str(data.get("status")) != "1":
        _LOGGER.debug("AMap reverse geocode error: %s", data.get("info"))
        return None

    regeocode = data.get("regeocode") or {}
    address = regeocode.get("formatted_address")
    if isinstance(address, list):  # AMap returns [] when no result
        return None
    return address or None


async def _osm(hass: HomeAssistant, latitude: float, longitude: float) -> str | None:
    """Query Nominatim, never faster than one request per OSM_MIN_INTERVAL."""
    global _osm_last_request

    session = async_get_clientsession(hass)
    params = {
        "format": "jsonv2",
        "lat": f"{latitude:.6f}",
        "lon": f"{longitude:.6f}",
        "zoom": 18,
        "accept-language": "zh-CN,zh,en",
    }
    headers = {"User-Agent": "HomeAssistant-huawei_find/0.5.0"}

    async with _OSM_LOCK:
        wait = OSM_MIN_INTERVAL - (time.monotonic() - _osm_last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _osm_last_request = time.monotonic()
        try:
            async with session.get(
                OSM_URL, params=params, headers=headers, timeout=_TIMEOUT
            ) as response:
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("OSM reverse geocode request failed: %s", err)
            return None

    if "error" in data:
        _LOGGER.debug("OSM reverse geocode error: %s", data["error"])
        return None
    return data.get("display_name") or None

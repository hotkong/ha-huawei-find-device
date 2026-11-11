"""Coordinate conversion and provider coordinate-system tests."""

import unittest
from unittest.mock import patch

from custom_components.huawei_find.api import gcj02_to_wgs84, wgs84_to_gcj02
from custom_components.huawei_find.geocode import _amap, _huawei, _osm

LAT, LNG = 31.311681, 120.608955


class ConversionTests(unittest.TestCase):
    def test_roundtrip_is_within_a_few_meters(self):
        glng, glat = wgs84_to_gcj02(LNG, LAT)
        back_lng, back_lat = gcj02_to_wgs84(glng, glat)
        self.assertAlmostEqual(back_lat, LAT, delta=1e-5)
        self.assertAlmostEqual(back_lng, LNG, delta=1e-5)

    def test_offset_matches_china_shift(self):
        glng, glat = wgs84_to_gcj02(LNG, LAT)
        self.assertNotEqual((glat, glng), (LAT, LNG))
        # Roughly a few hundred metres in China
        self.assertGreater(abs(glng - LNG), 0.002)
        self.assertGreater(abs(glat - LAT), 0.0005)

    def test_outside_china_is_unchanged(self):
        self.assertEqual(wgs84_to_gcj02(2.3522, 48.8566), (2.3522, 48.8566))


class FakeResponse:
    def __init__(self, data):
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._data


class FakeSession:
    def __init__(self, get_data=None, post_data=None):
        self.get_data = get_data or {}
        self.post_data = post_data or {}
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append(("get", url, params))
        return FakeResponse(self.get_data)

    def post(self, url, params=None, json=None, **kwargs):
        self.calls.append(("post", url, params, json))
        return FakeResponse(self.post_data)


class ProviderCoordinateTests(unittest.IsolatedAsyncioTestCase):
    async def test_huawei_receives_gcj02(self):
        glng, glat = wgs84_to_gcj02(LNG, LAT)
        session = FakeSession(post_data={"returnCode": "0", "addressDescription": "X"})
        with patch(
            "custom_components.huawei_find.geocode.async_get_clientsession",
            return_value=session,
        ):
            await _huawei(None, LAT, LNG)
        location = session.calls[0][3]["location"]
        self.assertAlmostEqual(location["latitude"], glat, places=6)
        self.assertAlmostEqual(location["longitude"], glng, places=6)

    async def test_amap_receives_gcj02(self):
        glng, glat = wgs84_to_gcj02(LNG, LAT)
        session = FakeSession(get_data={"status": "1", "regeocode": {"formatted_address": "X"}})
        with patch(
            "custom_components.huawei_find.geocode.async_get_clientsession",
            return_value=session,
        ):
            await _amap(None, "key", LAT, LNG)
        self.assertEqual(session.calls[0][2]["location"], f"{glng:.6f},{glat:.6f}")

    async def test_osm_keeps_wgs84(self):
        session = FakeSession(get_data={"display_name": "X"})
        with patch(
            "custom_components.huawei_find.geocode.async_get_clientsession",
            return_value=session,
        ):
            await _osm(None, LAT, LNG)
        params = session.calls[0][2]
        self.assertEqual(params["lat"], f"{LAT:.6f}")
        self.assertEqual(params["lon"], f"{LNG:.6f}")


if __name__ == "__main__":
    unittest.main()

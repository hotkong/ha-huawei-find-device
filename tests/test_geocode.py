"""Reverse geocoding provider chain and routing tests."""

import unittest
from unittest.mock import AsyncMock, patch

from custom_components.huawei_find import geocode


class ProviderChainTests(unittest.TestCase):
    def test_auto_without_key(self):
        self.assertEqual(
            geocode._provider_chain("auto", ""), ["huawei", "osm"]
        )

    def test_auto_with_key(self):
        self.assertEqual(
            geocode._provider_chain("auto", "abc"),
            ["huawei", "amap", "osm"],
        )

    def test_explicit_provider(self):
        self.assertEqual(geocode._provider_chain("osm", "abc"), ["osm"])


class CacheTests(unittest.TestCase):
    def test_cache_is_trimmed_to_its_bound(self):
        geocode._CACHE.clear()
        for index in range(geocode._CACHE_MAX_ENTRIES + 20):
            geocode._CACHE[(f"k{index}",)] = ("addr", float(index))
        geocode._trim_cache()
        self.assertLessEqual(len(geocode._CACHE), geocode._CACHE_MAX_ENTRIES)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_huawei_is_used_for_huawei_provider(self):
        with patch.object(geocode, "_huawei", AsyncMock(return_value="HW")), \
             patch.object(geocode, "_osm", AsyncMock(return_value="OSM")):
            result = await geocode.async_reverse_geocode(
                None, "huawei", "", 11.001, 21.001
            )
        self.assertEqual(result, "HW")

    async def test_auto_prefers_huawei(self):
        hw = AsyncMock(return_value="HW")
        amap = AsyncMock(return_value="AMAP")
        osm = AsyncMock(return_value="OSM")
        with patch.object(geocode, "_huawei", hw), \
             patch.object(geocode, "_amap", amap), \
             patch.object(geocode, "_osm", osm):
            result = await geocode.async_reverse_geocode(
                None, "auto", "key", 11.002, 21.002
            )
        self.assertEqual(result, "HW")
        amap.assert_not_called()
        osm.assert_not_called()

    async def test_auto_falls_back_to_amap(self):
        with patch.object(geocode, "_huawei", AsyncMock(return_value=None)), \
             patch.object(geocode, "_amap", AsyncMock(return_value="AMAP")), \
             patch.object(geocode, "_osm", AsyncMock(return_value="OSM")):
            result = await geocode.async_reverse_geocode(
                None, "auto", "key", 11.003, 21.003
            )
        self.assertEqual(result, "AMAP")

    async def test_auto_falls_back_to_osm(self):
        with patch.object(geocode, "_huawei", AsyncMock(return_value=None)), \
             patch.object(geocode, "_osm", AsyncMock(return_value="OSM")):
            result = await geocode.async_reverse_geocode(
                None, "auto", "", 11.004, 21.004
            )
        self.assertEqual(result, "OSM")

    async def test_auto_without_key_skips_amap(self):
        amap = AsyncMock(return_value="AMAP")
        with patch.object(geocode, "_huawei", AsyncMock(return_value=None)), \
             patch.object(geocode, "_amap", amap), \
             patch.object(geocode, "_osm", AsyncMock(return_value=None)):
            result = await geocode.async_reverse_geocode(
                None, "auto", "", 11.005, 21.005
            )
        self.assertIsNone(result)
        amap.assert_not_called()


class ProviderPayloadTests(unittest.IsolatedAsyncioTestCase):
    class _Response:
        def __init__(self, payload):
            self.payload = payload

        async def json(self, content_type=None):
            if isinstance(self.payload, Exception):
                raise self.payload
            return self.payload

    class _Context:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *args):
            return False

    class _Session:
        def __init__(self, response):
            self.response = response

        def post(self, *args, **kwargs):
            return ProviderPayloadTests._Context(self.response)

    async def test_huawei_non_dict_payload_is_ignored(self):
        session = self._Session(self._Response(["not", "a", "dict"]))
        with patch.object(
            geocode, "async_get_clientsession", return_value=session
        ):
            self.assertIsNone(await geocode._huawei(None, 31.2, 121.4))

    async def test_huawei_bad_json_is_ignored(self):
        session = self._Session(self._Response(ValueError("html")))
        with patch.object(
            geocode, "async_get_clientsession", return_value=session
        ):
            self.assertIsNone(await geocode._huawei(None, 31.2, 121.4))


if __name__ == "__main__":
    unittest.main()

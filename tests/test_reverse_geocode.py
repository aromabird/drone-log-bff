"""`GET /api/reverse-geocode` のオフラインテスト。

Google Geocoding API へは接続せず、HTTP 呼び出しと応答の変換だけを検証する。
実行: `.venv/bin/python tests/test_reverse_geocode.py -v`
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)
API_KEY = "test-google-maps-key"
LAT = 35.681236
LNG = 139.767125

GOOGLE_OK = {
    "status": "OK",
    "results": [
        {
            "formatted_address": "日本、〒100-0005 東京都千代田区丸の内１丁目",
            "place_id": "ChIJ-test-place",
            "geometry": {"location": {"lat": LAT, "lng": LNG}},
            "address_components": [
                {"long_name": "日本", "short_name": "JP", "types": ["country"]},
                {"long_name": "100-0005", "short_name": "100-0005", "types": ["postal_code"]},
                {
                    "long_name": "東京都",
                    "short_name": "東京都",
                    "types": ["administrative_area_level_1"],
                },
                {"long_name": "千代田区", "short_name": "千代田区", "types": ["locality"]},
                {
                    "long_name": "丸の内１丁目",
                    "short_name": "丸の内１丁目",
                    "types": ["sublocality_level_1"],
                },
            ],
        }
    ],
}


class FakeResponse:
    """requests.Response の最小限の代用。"""

    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class ReverseGeocodeTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(main, "GOOGLE_MAPS_API_KEY", API_KEY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self, lat=LAT, lng=LNG):
        return client.get("/api/reverse-geocode", params={"lat": lat, "lng": lng})

    def _assert_error(self, payload, expected_status: int, expected_code: str) -> None:
        with patch("main.requests.get", return_value=FakeResponse(payload)):
            response = self._get()
        self.assertEqual(response.status_code, expected_status, response.text)
        detail = response.json().get("detail")
        self.assertIsInstance(detail, dict, response.text)
        self.assertEqual(detail.get("code"), expected_code, response.text)
        self.assertTrue(detail.get("message"), response.text)

    def test_success_cleans_address_and_returns_coordinates(self) -> None:
        with patch("main.requests.get", return_value=FakeResponse(GOOGLE_OK)) as get:
            response = self._get()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIs(body["ok"], True)
        self.assertEqual(body["address"], "東京都千代田区丸の内１丁目")
        self.assertEqual(
            body["formatted_address"], "日本、〒100-0005 東京都千代田区丸の内１丁目"
        )
        self.assertAlmostEqual(body["lat"], LAT, places=6)
        self.assertAlmostEqual(body["lng"], LNG, places=6)
        self.assertEqual(body["place_id"], "ChIJ-test-place")
        self.assertEqual(body["source"], "google")
        self.assertTrue(body["fetched_at"])
        # キーはレスポンスへ出さない。
        self.assertNotIn(API_KEY, response.text)

        args, kwargs = get.call_args
        self.assertEqual(args[0], main.GOOGLE_GEOCODE_URL)
        self.assertEqual(kwargs["params"]["key"], API_KEY)
        self.assertEqual(kwargs["params"]["language"], "ja")
        self.assertEqual(kwargs["params"]["latlng"], "35.681236,139.767125")
        self.assertEqual(kwargs["timeout"], main.GOOGLE_GEOCODE_TIMEOUT)

    def test_falls_back_to_address_components(self) -> None:
        payload = {
            "status": "OK",
            "results": [
                {
                    "formatted_address": "",
                    "address_components": [
                        {"long_name": "千葉県", "types": ["administrative_area_level_1"]},
                        {"long_name": "香取市", "types": ["locality"]},
                        {"long_name": "小見川", "types": ["sublocality_level_1"]},
                        {"long_name": "日本", "types": ["country"]},
                    ],
                    "geometry": {"location": {"lat": 35.9, "lng": 140.5}},
                }
            ],
        }
        with patch("main.requests.get", return_value=FakeResponse(payload)):
            response = self._get()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["address"], "千葉県香取市小見川")

    def test_missing_geometry_keeps_request_coordinates(self) -> None:
        payload = {
            "status": "OK",
            "results": [{"formatted_address": "日本、東京都千代田区丸の内１丁目"}],
        }
        with patch("main.requests.get", return_value=FakeResponse(payload)):
            response = self._get()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertAlmostEqual(response.json()["lat"], LAT, places=6)
        self.assertAlmostEqual(response.json()["lng"], LNG, places=6)

    def test_missing_api_key(self) -> None:
        with patch.object(main, "GOOGLE_MAPS_API_KEY", ""):
            with patch("main.requests.get") as get:
                response = self._get()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"]["code"], "geocode_not_configured")
        get.assert_not_called()

    def test_zero_results(self) -> None:
        self._assert_error({"status": "ZERO_RESULTS", "results": []}, 404, "geocode_not_found")

    def test_ok_without_results(self) -> None:
        self._assert_error({"status": "OK", "results": []}, 404, "geocode_not_found")

    def test_request_denied(self) -> None:
        self._assert_error({"status": "REQUEST_DENIED"}, 503, "geocode_auth_error")

    def test_over_query_limit(self) -> None:
        self._assert_error({"status": "OVER_QUERY_LIMIT"}, 503, "geocode_rate_limited")

    def test_invalid_request(self) -> None:
        self._assert_error({"status": "INVALID_REQUEST"}, 502, "geocode_invalid_request")

    def test_unknown_status(self) -> None:
        self._assert_error({"status": "UNKNOWN_ERROR"}, 502, "geocode_upstream_error")

    def test_timeout(self) -> None:
        with patch("main.requests.get", side_effect=main.requests.Timeout("boom")):
            response = self._get()
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["detail"]["code"], "geocode_timeout")

    def test_connection_error(self) -> None:
        with patch("main.requests.get", side_effect=main.requests.ConnectionError("boom")):
            response = self._get()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["detail"]["code"], "geocode_upstream_error")

    def test_invalid_json(self) -> None:
        with patch("main.requests.get", return_value=FakeResponse(ValueError("bad json"))):
            response = self._get()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["detail"]["code"], "geocode_invalid_response")

    def test_non_object_json(self) -> None:
        with patch("main.requests.get", return_value=FakeResponse(["unexpected"])):
            response = self._get()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["detail"]["code"], "geocode_invalid_response")

    def test_latitude_out_of_range(self) -> None:
        with patch("main.requests.get") as get:
            response = self._get(lat=95)
        self.assertEqual(response.status_code, 422, response.text)
        get.assert_not_called()

    def test_longitude_out_of_range(self) -> None:
        with patch("main.requests.get") as get:
            response = self._get(lng=181)
        self.assertEqual(response.status_code, 422, response.text)
        get.assert_not_called()


class CleanAddressTests(unittest.TestCase):
    def test_removes_country_and_postal_code(self) -> None:
        cases = {
            "日本、〒100-0005 東京都千代田区丸の内１丁目": "東京都千代田区丸の内１丁目",
            "日本, 〒060-0001 北海道札幌市中央区北一条西１丁目": "北海道札幌市中央区北一条西１丁目",
            "〒100-0005 東京都千代田区丸の内１丁目": "東京都千代田区丸の内１丁目",
            "日本、東京都千代田区": "東京都千代田区",
            "東京都千代田区丸の内１丁目": "東京都千代田区丸の内１丁目",
            "東京都千代田区, 日本": "東京都千代田区",
            "": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(main._clean_japanese_address(raw), expected)

    def test_builds_address_from_components(self) -> None:
        components = [
            {"long_name": "東京都", "types": ["administrative_area_level_1"]},
            {"long_name": "千代田区", "types": ["locality"]},
            {"long_name": "丸の内", "types": ["sublocality_level_1"]},
            {"long_name": "丸の内", "types": ["neighborhood"]},
            {"long_name": "1-1", "types": ["street_number"]},
        ]
        self.assertEqual(main._address_from_components(components), "東京都千代田区丸の内")


if __name__ == "__main__":
    unittest.main(verbosity=2)

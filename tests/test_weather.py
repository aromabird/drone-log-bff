"""`GET /api/weather`（気象庁アメダス優先）のオフラインテスト。

気象庁・Open-Meteo へは接続せず、`requests.get` を差し替えて

- アメダスの観測所表・観測値の解釈（品質フラグ・風向・天候の推定）
- 雨量計のみの観測所しか近くに無いときの観測所選択
- 最新時刻のファイルが 404 のときの 10 分前スロットへの後退
- 気象庁 → Open-Meteo のフォールバック、キャッシュ、参考値（stale）
- 取得元ごとの時間予算（主系が遅いときも代替へ必ず回す）
- 入力検証とエラーの構造化

を検証する。実行: `.venv/bin/python tests/test_weather.py -v`
"""

from __future__ import annotations

import json
import socketserver
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)

# 東京付近の座標（テスト内の観測所は数百m〜数km の範囲に置く）。
LAT = 35.7000
LON = 139.7000

LATEST_TIME = "2026-11-15T10:30:00+09:00"
LATEST_TS = "20261115103000"
PREVIOUS_TS = "20261115102000"

# 観測所表（`amedastable.json` 相当）。緯度・経度は [度, 分] で返る。
AMEDAS_TABLE: dict[str, dict[str, Any]] = {
    "1001": {"lat": [35, 42.0], "lon": [139, 42.0], "kjName": "雨量計A"},
    "1002": {"lat": [35, 43.2], "lon": [139, 42.0], "kjName": "風観測B"},
    "1003": {"lat": [35, 44.4], "lon": [139, 42.0], "kjName": "遠方C"},
    # 座標が欠けている観測所は表から除外される。
    "1004": {"lat": [], "lon": [], "kjName": "欠測D"},
}


class FakeResponse:
    """`requests.Response` の最小限の代用。"""

    def __init__(
        self,
        json_data: Any = None,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._json = json_data
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def make_requests_get(
    observations: dict[str, Any] | None = None,
    *,
    latest_text: str = LATEST_TIME,
    latest_status: int = 200,
    table: dict[str, Any] | None = None,
    map_status: dict[str, int] | None = None,
) -> Any:
    """気象庁の URL を URL ごとに振り分ける `requests.get` の代用を作る。

    `map_status` に `{タイムスタンプ: ステータス}` を渡すと、その時刻の
    観測値ファイルだけ任意のステータス（404 など）を返せる。
    """

    map_url_prefix = main.JMA_AMEDAS_MAP_URL.split("{ts}")[0]
    payload = observations if observations is not None else {}
    statuses = map_status or {}
    calls: list[str] = []

    def fake_get(url: str, params: Any = None, timeout: Any = None) -> FakeResponse:
        calls.append(url)
        if url == main.JMA_AMEDAS_LATEST_URL:
            return FakeResponse(status_code=latest_status, text=latest_text)
        if url == main.JMA_AMEDAS_TABLE_URL:
            return FakeResponse(json_data=dict(table or AMEDAS_TABLE))
        if url.startswith(map_url_prefix):
            stamp = url[len(map_url_prefix):].split(".")[0]
            status = statuses.get(stamp, 200)
            return FakeResponse(json_data=payload, status_code=status)
        raise AssertionError(f"unexpected url: {url}")

    fake_get.calls = calls  # type: ignore[attr-defined]
    return fake_get


def observations_for() -> dict[str, Any]:
    """風速・気温は 1002、降水量だけなら 1001 という観測値一式。"""

    return {
        "1001": {"precipitation1h": [0.0, 0]},
        "1002": {
            "wind": [4.5, 0],
            "windDirection": [16, 0],
            "temp": [12.3, 0],
            "humidity": [58, 0],
            "precipitation1h": [0.0, 0],
            "sun1h": [0.8, 0],
        },
    }


class WeatherHelperTests(unittest.TestCase):
    """純粋関数（丸め・方位・アメダスの解釈）のテスト。"""

    def test_round_optional(self) -> None:
        self.assertIsNone(main._round_optional(None))
        self.assertIsNone(main._round_optional("abc"))
        self.assertIsNone(main._round_optional(float("nan")))
        self.assertEqual(main._round_optional(3.14159), 3.1)
        self.assertEqual(main._round_optional("2.25", 0), 2.0)

    def test_bearing_label(self) -> None:
        self.assertEqual(main._bearing_label(0), "北")
        self.assertEqual(main._bearing_label(90), "東")
        self.assertEqual(main._bearing_label(180), "南")
        self.assertEqual(main._bearing_label(270), "西")
        self.assertEqual(main._bearing_label(350), "北")
        self.assertEqual(main._bearing_label(360), "北")
        self.assertIsNone(main._bearing_label(None))

    def test_amedas_wind_label(self) -> None:
        self.assertEqual(main._amedas_wind_label(0), "静穏")
        self.assertEqual(main._amedas_wind_label(1), "北北東")
        self.assertEqual(main._amedas_wind_label(16), "北")
        self.assertIsNone(main._amedas_wind_label(17))
        self.assertIsNone(main._amedas_wind_label(None))

    def test_amedas_coordinate(self) -> None:
        self.assertAlmostEqual(main._amedas_coordinate([35, 41.2]), 35.6867, places=3)
        self.assertAlmostEqual(main._amedas_coordinate([-33, 52.0]), -33.8667, places=3)
        self.assertIsNone(main._amedas_coordinate([35]))
        self.assertIsNone(main._amedas_coordinate(None))

    def test_parse_amedas_table_skips_broken_entries(self) -> None:
        table = main._parse_amedas_table(AMEDAS_TABLE)
        self.assertEqual(sorted(table), ["1001", "1002", "1003"])
        self.assertEqual(table["1002"]["name"], "風観測B")
        self.assertAlmostEqual(table["1002"]["lat"], 35.72, places=3)

    def test_amedas_value_drops_bad_quality_flags(self) -> None:
        self.assertEqual(main._amedas_value({"wind": [4.5, 0]}, "wind"), 4.5)
        self.assertIsNone(main._amedas_value({"wind": [4.5, 1]}, "wind"))
        self.assertIsNone(main._amedas_value({"wind": []}, "wind"))
        self.assertIsNone(main._amedas_value({}, "wind"))

    def test_amedas_weather_text(self) -> None:
        self.assertEqual(main._amedas_weather_text(0.0, None, 0.8, 12.0), ("晴れ", 1))
        self.assertEqual(main._amedas_weather_text(0.0, None, 0.0, 12.0), ("曇り", 3))
        self.assertEqual(main._amedas_weather_text(2.0, None, None, 12.0), ("雨", 63))
        self.assertEqual(main._amedas_weather_text(2.0, None, None, 0.5), ("雪", 73))
        self.assertEqual(main._amedas_weather_text(0.1, None, None, 12.0), ("弱い雨", 61))
        self.assertEqual(main._amedas_weather_text(None, 3.0, None, 12.0), ("雪", 73))

    def test_pick_amedas_station_prefers_wind(self) -> None:
        table = main._parse_amedas_table(AMEDAS_TABLE)
        candidates = main._nearest_amedas_stations(LAT, LON, table, limit=3)
        station_id, station, _distance = main._pick_amedas_station(
            candidates, observations_for()
        )
        # 最寄りは雨量計A（1001）だが、風速を持つ 1002 を選ぶ。
        self.assertEqual(candidates[0][0], "1001")
        self.assertEqual(station_id, "1002")
        self.assertEqual(station["name"], "風観測B")

    def test_pick_amedas_station_falls_back_to_nearest(self) -> None:
        table = main._parse_amedas_table(AMEDAS_TABLE)
        candidates = main._nearest_amedas_stations(LAT, LON, table, limit=3)
        station_id, _station, _distance = main._pick_amedas_station(candidates, {})
        self.assertEqual(station_id, "1001")

    def test_pick_amedas_station_requires_candidates(self) -> None:
        with self.assertRaises(main.WeatherFetchError):
            main._pick_amedas_station([], {})


class AmedasFetchTests(unittest.TestCase):
    """気象庁アメダスの取得（HTTP はモック）のテスト。"""

    def setUp(self) -> None:
        main.clear_weather_cache()
        main._AMEDAS_TABLE.clear()
        main._AMEDAS_TABLE_FETCHED_AT = 0.0
        self.addCleanup(main.clear_weather_cache)

    def test_fetch_amedas_uses_wind_station(self) -> None:
        fake_get = make_requests_get(observations_for())
        deadline = time.monotonic() + 5.0
        with patch("main.requests.get", side_effect=fake_get):
            payload = main._fetch_jma_amedas(LAT, LON, deadline)

        self.assertEqual(payload["source"], "jma-amedas")
        self.assertEqual(payload["station"]["id"], "1002")
        self.assertEqual(payload["station"]["name"], "風観測B")
        self.assertIn("風観測B", payload["source_label"])
        weather = payload["weather"]
        self.assertEqual(weather["text"], "晴れ")
        self.assertEqual(weather["code"], 1)
        self.assertEqual(weather["wind_speed"], 4.5)
        self.assertEqual(weather["wind_direction"], "北")
        self.assertEqual(weather["temperature"], 12.3)
        self.assertEqual(weather["humidity"], 58.0)
        self.assertIsNone(weather["wind_gust"])
        self.assertEqual(weather["observed_at"], "2026-11-15T10:30:00+09:00")
        self.assertTrue(
            any("雨量計のみ" in note for note in payload["notes"]), payload["notes"]
        )

    def test_fetch_amedas_infers_snow_and_drops_flagged_values(self) -> None:
        observations = {
            "1002": {
                "snow": [3.0, 0],
                "precipitation1h": [1.0, 1],
                "temp": [0.5, 0],
                "wind": [2.0, 0],
                "windDirection": [0, 0],
            }
        }
        fake_get = make_requests_get(observations)
        deadline = time.monotonic() + 5.0
        with patch("main.requests.get", side_effect=fake_get):
            payload = main._fetch_jma_amedas(LAT, LON, deadline)

        weather = payload["weather"]
        self.assertEqual(weather["text"], "雪")
        self.assertEqual(weather["code"], 73)
        self.assertIsNone(weather["precipitation"])  # 品質フラグ 1 は欠測扱い
        self.assertEqual(weather["wind_direction"], "静穏")

    def test_fetch_amedas_map_falls_back_to_previous_slot(self) -> None:
        fake_get = make_requests_get(observations_for(), map_status={LATEST_TS: 404})
        deadline = time.monotonic() + 5.0
        with patch("main.requests.get", side_effect=fake_get):
            observations, timestamp = main._fetch_amedas_map(deadline)

        self.assertEqual(timestamp, PREVIOUS_TS)
        self.assertIn("1002", observations)

    def test_fetch_amedas_map_raises_when_all_slots_missing(self) -> None:
        fake_get = make_requests_get(
            observations_for(),
            map_status={LATEST_TS: 404, PREVIOUS_TS: 404, "20261115101000": 404},
        )
        deadline = time.monotonic() + 5.0
        with patch("main.requests.get", side_effect=fake_get):
            with self.assertRaises(main.WeatherFetchError) as ctx:
                main._fetch_amedas_map(deadline)
        self.assertEqual(ctx.exception.code, "weather_not_found")

    def test_latest_time_without_seconds_is_rejected(self) -> None:
        fake_get = make_requests_get(
            observations_for(), latest_text="2026-11-15T10:30"
        )
        deadline = time.monotonic() + 5.0
        with patch("main.requests.get", side_effect=fake_get):
            with self.assertRaises(main.WeatherFetchError) as ctx:
                main._amedas_timestamp(deadline)
        self.assertEqual(ctx.exception.code, "weather_invalid_response")


class WeatherCacheTests(unittest.TestCase):
    """キャッシュ・参考値への縮退のテスト。"""

    def setUp(self) -> None:
        main.clear_weather_cache()
        self.addCleanup(main.clear_weather_cache)

    @staticmethod
    def payload() -> dict[str, Any]:
        return {
            "source": "jma-amedas",
            "source_label": "気象庁 アメダス（風観測B観測所・約0.5km）",
            "weather": {
                "text": "曇り",
                "code": 3,
                "temperature": 12.3,
                "humidity": 58.0,
                "precipitation": 0.0,
                "wind_speed": 4.5,
                "wind_gust": None,
                "wind_direction": "北",
                "observed_at": "2026-11-15T10:30:00+09:00",
            },
            "station": {"id": "1002", "name": "風観測B", "distance_km": 0.5},
            "notes": ["テスト用"],
            "attempts": ["jma-amedas:ok"],
        }

    def test_cache_hit_skips_provider(self) -> None:
        with patch("main._collect_weather", return_value=self.payload()) as collect:
            first = main._request_weather(LAT, LON)
            second = main._request_weather(LAT, LON)

        self.assertEqual(collect.call_count, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertFalse(second["stale"])
        self.assertEqual(second["elapsed_ms"], 0)
        self.assertEqual(second["weather"]["wind_speed"], 4.5)

    def test_stale_cache_is_returned_with_note(self) -> None:
        key = main._weather_cache_key(LAT, LON)
        main._weather_cache_put(key, self.payload())
        # TTL（既定120秒）を超え、参考値の上限（既定1800秒）以内にする。
        main._WEATHER_CACHE[key]["_ts"] = time.monotonic() - 300

        error = main.WeatherFetchError(
            "weather_unavailable", "気象庁が混雑しています。", transient=False
        )
        with patch("main._collect_weather", side_effect=error):
            payload = main._request_weather(LAT, LON)

        self.assertTrue(payload["cached"])
        self.assertTrue(payload["stale"])
        self.assertTrue(
            any("参考値" in note for note in payload["notes"]), payload["notes"]
        )

    def test_expired_cache_is_not_used_when_provider_fails(self) -> None:
        key = main._weather_cache_key(LAT, LON)
        main._weather_cache_put(key, self.payload())
        main._WEATHER_CACHE[key]["_ts"] = (
            time.monotonic() - main.WEATHER_STALE_MAX_SECONDS - 60
        )

        error = main.WeatherFetchError(
            "weather_unavailable", "気象庁が混雑しています。", transient=False
        )
        with patch("main._collect_weather", side_effect=error):
            with self.assertRaises(main.WeatherFetchError):
                main._request_weather(LAT, LON)


class WeatherApiTests(unittest.TestCase):
    """`GET /api/weather` のルーティング・フォールバック・検証のテスト。"""

    def setUp(self) -> None:
        main.clear_weather_cache()
        self.addCleanup(main.clear_weather_cache)

    @staticmethod
    def open_meteo_payload() -> dict[str, Any]:
        return {
            "source": "open-meteo",
            "source_label": "Open-Meteo",
            "weather": {
                "text": "薄曇り",
                "code": 2,
                "temperature": 11.0,
                "humidity": 70.0,
                "precipitation": 0.0,
                "wind_speed": 3.0,
                "wind_gust": 6.0,
                "wind_direction": "北西",
                "observed_at": "2026-11-15T10:30",
            },
            "station": None,
            "notes": ["Open-Meteo の現在値（予報モデル）を使用しています。"],
        }

    def test_falls_back_to_open_meteo(self) -> None:
        jma_error = main.WeatherFetchError(
            "weather_invalid_response",
            "気象庁の応答を解釈できませんでした。",
            transient=False,
        )
        with patch("main._fetch_jma_amedas", side_effect=jma_error):
            with patch(
                "main._fetch_open_meteo", return_value=self.open_meteo_payload()
            ):
                response = client.get("/api/weather", params={"lat": LAT, "lng": LON})

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["source"], "open-meteo")
        self.assertIsNone(data["station"])
        self.assertEqual(data["weather"]["code"], 2)
        self.assertEqual(
            data["attempts"],
            ["jma-amedas:weather_invalid_response", "open-meteo:ok"],
        )
        self.assertFalse(data["cached"])
        self.assertFalse(data["stale"])
        self.assertTrue(data["fetched_at"].endswith("+09:00"))

    def test_returns_structured_error_when_all_providers_fail(self) -> None:
        error = main.WeatherFetchError(
            "weather_unavailable",
            "気象データが混雑しています。時間をおいて再試行してください。",
            transient=False,
            status_code=503,
        )
        with patch("main._fetch_jma_amedas", side_effect=error):
            with patch("main._fetch_open_meteo", side_effect=error):
                response = client.get("/api/weather", params={"lat": LAT, "lng": LON})

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"]["code"], "weather_unavailable")

    def test_rejects_out_of_range_coordinates(self) -> None:
        self.assertEqual(
            client.get("/api/weather", params={"lat": 123, "lng": LON}).status_code,
            422,
        )
        self.assertEqual(
            client.get("/api/weather", params={"lat": LAT, "lng": 200}).status_code,
            422,
        )

    def test_requires_coordinates(self) -> None:
        self.assertEqual(client.get("/api/weather").status_code, 422)


OPEN_METEO_BODY: dict[str, Any] = {
    "current": {
        "time": "2026-11-15T11:00",
        "temperature_2m": 11.0,
        "relative_humidity_2m": 70,
        "precipitation": 0.0,
        "weather_code": 2,
        "wind_speed_10m": 3.0,
        "wind_gusts_10m": 6.0,
        "wind_direction_10m": 315,
    }
}


def jma_payload() -> dict[str, Any]:
    """気象庁側が成功したときの取得結果（再試行のテスト用）。"""

    return {
        "source": "jma-amedas",
        "source_label": "気象庁 アメダス（風観測B観測所・約0.5km）",
        "weather": {
            "text": "曇り",
            "code": 3,
            "temperature": 12.3,
            "humidity": 58.0,
            "precipitation": 0.0,
            "wind_speed": 4.5,
            "wind_gust": None,
            "wind_direction": "北",
            "observed_at": "2026-11-15T10:30:00+09:00",
        },
        "station": {"id": "1002", "name": "風観測B", "distance_km": 0.5},
        "notes": ["テスト用"],
    }


class _StubHTTPServer(ThreadingHTTPServer):
    """`server_bind` の逆引き（getfqdn）で待たされないサーバー。

    標準の `HTTPServer.server_bind` は `socket.getfqdn` を呼ぶため、DNS が
    遅い環境ではテストが十数秒待たされる。127.0.0.1 固定なので逆引きしない。
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class WeatherStubServer:
    """気象 API の代用になるローカル HTTP サーバー。

    実ソケットを使うことで、`requests` のタイムアウト挙動まで含めて
    「遅い気象庁 → Open-Meteo へフォールバック」を再現する。
    `/latest_time.txt` だけ任意の秒数だけ遅らせられる。
    """

    def __init__(self, *, latest_delay: float = 0.0) -> None:
        self.latest_delay = latest_delay
        self.hits: dict[str, int] = {}
        self._server = _StubHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "WeatherStubServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def hit_count(self, prefix: str) -> int:
        return sum(count for path, count in self.hits.items() if path.startswith(prefix))

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server の命名
                stub.hits[self.path] = stub.hits.get(self.path, 0) + 1
                if self.path.startswith("/latest_time"):
                    time.sleep(stub.latest_delay)
                    body = LATEST_TIME.encode()
                elif self.path.startswith("/table.json"):
                    body = json.dumps(AMEDAS_TABLE).encode()
                elif self.path.startswith("/map/"):
                    body = json.dumps(observations_for()).encode()
                elif self.path.startswith("/open-meteo"):
                    body = json.dumps(OPEN_METEO_BODY).encode()
                else:
                    self.send_error(404)
                    return
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    # クライアントがタイムアウトで切った後（想定内）。
                    pass

            def log_message(self, *_args: object) -> None:
                return

        return Handler


class WeatherBudgetTests(unittest.TestCase):
    """取得元ごとの時間予算と再試行ポリシーのテスト。"""

    def test_primary_budget_keeps_time_for_fallback(self) -> None:
        now = time.monotonic()
        provider_deadline = main._weather_provider_deadline(now + 8.0, 8.0, False)

        # 既定: 予算 4 秒。代替のために 3 秒以上を残す。
        self.assertAlmostEqual(
            provider_deadline - now, main.WEATHER_PROVIDER_BUDGET_SECONDS, delta=0.05
        )
        self.assertGreaterEqual(
            (now + 8.0) - provider_deadline, main.WEATHER_FALLBACK_RESERVE_SECONDS - 0.05
        )

    def test_short_remaining_is_split_between_providers(self) -> None:
        now = time.monotonic()
        provider_deadline = main._weather_provider_deadline(now + 2.0, 2.0, False)

        # 残り 2 秒なら半分（1 秒）を主系、残りを代替に割り当てる。
        self.assertAlmostEqual(provider_deadline - now, 1.0, delta=0.05)

    def test_last_provider_can_use_full_deadline(self) -> None:
        target = time.monotonic() + 8.0

        self.assertEqual(main._weather_provider_deadline(target, 8.0, True), target)

    def test_timeout_is_retried_only_for_last_provider(self) -> None:
        timeout = main.WeatherFetchError(
            "weather_timeout", "応答がありません。", transient=True, status_code=504
        )
        busy = main.WeatherFetchError(
            "weather_unavailable", "混雑しています。", transient=True, status_code=503
        )

        # 主系（retry_timeouts=False）はタイムアウトを再試行しない。
        self.assertFalse(main._is_transient_weather_error(timeout, False))
        # 最後の取得元は残り時間の中で再試行してよい。
        self.assertTrue(main._is_transient_weather_error(timeout, True))
        # 429・5xx はすぐ返る失敗なので主系でも再試行する。
        self.assertTrue(main._is_transient_weather_error(busy, False))


class WeatherTimeBudgetTests(unittest.TestCase):
    """主系が遅い／落ちているときに代替で応答することを実ソケットで確かめる。"""

    def setUp(self) -> None:
        main.clear_weather_cache()
        self.addCleanup(main.clear_weather_cache)

    def test_slow_jma_falls_back_to_open_meteo_without_5xx(self) -> None:
        # 気象庁の latest_time.txt だけ 1.5 秒遅らせ、応答タイムアウトを 0.3 秒にする。
        with WeatherStubServer(latest_delay=1.5) as stub:
            with patch.object(
                main, "JMA_AMEDAS_LATEST_URL", f"{stub.base_url}/latest_time.txt"
            ), patch.object(
                main, "JMA_AMEDAS_TABLE_URL", f"{stub.base_url}/table.json"
            ), patch.object(
                main, "JMA_AMEDAS_MAP_URL", f"{stub.base_url}/map/{{ts}}.json"
            ), patch.object(
                main, "OPEN_METEO_URL", f"{stub.base_url}/open-meteo"
            ), patch.object(
                main, "WEATHER_JMA_TIMEOUT", (0.2, 0.3)
            ), patch.object(
                main, "WEATHER_OPEN_METEO_TIMEOUT", (0.2, 0.5)
            ), patch.object(
                main, "WEATHER_DEADLINE_SECONDS", 3.0
            ), patch.object(
                main, "WEATHER_PROVIDER_BUDGET_SECONDS", 1.0
            ), patch.object(
                main, "WEATHER_FALLBACK_RESERVE_SECONDS", 1.5
            ):
                started = time.monotonic()
                response = client.get(
                    "/api/weather", params={"lat": LAT, "lng": LON}
                )
                elapsed = time.monotonic() - started

            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(data["source"], "open-meteo")
            self.assertEqual(data["weather"]["wind_gust"], 6.0)
            self.assertEqual(
                data["attempts"], ["jma-amedas:weather_timeout", "open-meteo:ok"]
            )
            self.assertFalse(data["stale"])
            # 全体予算（3 秒）を超えずに返る。
            self.assertLess(elapsed, 3.0)
            # タイムアウトした主系は再試行しない（残り時間を代替へ回す）。
            self.assertEqual(stub.hit_count("/latest_time.txt"), 1)
            self.assertEqual(stub.hit_count("/open-meteo"), 1)

    def test_unreachable_jma_falls_back(self) -> None:
        # 接続できない URL（ポート 9 = discard）へ差し替える。
        with patch.object(
            main, "JMA_AMEDAS_TABLE_URL", "http://127.0.0.1:9/table.json"
        ), patch.object(
            main, "JMA_AMEDAS_LATEST_URL", "http://127.0.0.1:9/latest_time.txt"
        ):
            with patch(
                "main._fetch_open_meteo", return_value=jma_payload()
            ) as open_meteo:
                response = client.get("/api/weather", params={"lat": LAT, "lng": LON})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(open_meteo.call_count, 1)

    def test_primary_timeout_is_not_retried(self) -> None:
        timeout = main.WeatherFetchError(
            "weather_timeout", "気象庁の応答がありません。", transient=True, status_code=504
        )
        with patch("main._fetch_jma_amedas", side_effect=timeout) as jma:
            with patch("main._fetch_open_meteo", return_value=jma_payload()):
                response = client.get("/api/weather", params={"lat": LAT, "lng": LON})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(jma.call_count, 1)
        self.assertEqual(
            response.json()["attempts"], ["jma-amedas:weather_timeout", "open-meteo:ok"]
        )

    def test_transient_congestion_is_retried(self) -> None:
        busy = main.WeatherFetchError(
            "weather_unavailable", "気象庁が混雑しています。", transient=True, status_code=503
        )
        with patch("main._fetch_jma_amedas", side_effect=[busy, jma_payload()]) as jma:
            with patch("main._fetch_open_meteo") as open_meteo:
                response = client.get("/api/weather", params={"lat": LAT, "lng": LON})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(jma.call_count, 2)  # 1 回目 503 → 指数バックオフ後に成功
        open_meteo.assert_not_called()
        self.assertEqual(response.json()["attempts"], ["jma-amedas:ok"])

    def test_all_providers_timeout_returns_structured_error(self) -> None:
        timeout = main.WeatherFetchError(
            "weather_timeout", "気象データの応答がありません。", transient=True, status_code=504
        )
        with patch.object(main, "WEATHER_DEADLINE_SECONDS", 2.0), patch.object(
            main, "WEATHER_PROVIDER_BUDGET_SECONDS", 1.2
        ), patch.object(main, "WEATHER_FALLBACK_RESERVE_SECONDS", 0.8):
            with patch("main._fetch_jma_amedas", side_effect=timeout):
                with patch("main._fetch_open_meteo", side_effect=timeout):
                    started = time.monotonic()
                    response = client.get(
                        "/api/weather", params={"lat": LAT, "lng": LON}
                    )
                    elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["detail"]["code"], "weather_timeout")
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

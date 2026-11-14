"""`GET /api/check-infrastructure` のオフラインテスト。

OSM API / Overpass API へは接続せず、取得元の切り替えと「総処理時間の上限」
（応答が細切れに届いても deadline で打ち切る）を検証する。
実行: `.venv/bin/python tests/test_check_infrastructure.py -v`
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)

LAT = 35.681236
LON = 139.767125

# 中心から数百m以内に鉄塔1基・送電線1本がある最小の OSM XML。
OSM_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="35.681300" lon="139.767200"><tag k="power" v="tower"/></node>
  <node id="2" lat="35.681400" lon="139.767300"/>
  <node id="3" lat="35.681500" lon="139.767400"/>
  <way id="10"><nd ref="2"/><nd ref="3"/><tag k="power" v="line"/></way>
</osm>
"""


class FakeResponse:
    """requests.Response の最小限の代用（ストリーミング読み取り対応）。"""

    def __init__(
        self,
        body: bytes = b"",
        status_code: int = 200,
        chunks: list[bytes] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.status_code = status_code
        self._chunks = chunks if chunks is not None else [body]
        self._delay = delay
        self.content = body
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:  # noqa: ARG002
        for chunk in self._chunks:
            if self._delay:
                time.sleep(self._delay)
            yield chunk

    def close(self) -> None:
        self.closed = True


class CheckInfrastructureTests(unittest.TestCase):
    def setUp(self) -> None:
        # テスト間でサーキットブレーカーの状態を持ち越さない。
        main._OVERPASS_DOWN_UNTIL = 0.0

    def _get(self):
        return client.get(
            "/api/check-infrastructure",
            params={"lat": LAT, "lon": LON, "radius": 300},
        )

    def _assert_error(self, payload, expected_code: str) -> None:
        self.assertEqual(
            payload.get("detail", {}).get("code"), expected_code, payload
        )
        self.assertTrue(payload.get("detail", {}).get("message"))

    def test_osm_api_success(self) -> None:
        with patch(
            "main.requests.get", return_value=FakeResponse(OSM_XML)
        ), patch("main.requests.post") as post:
            response = self._get()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source"], "osm-api")
        self.assertEqual(body["towers_count"], 1)
        self.assertEqual(body["lines_count"], 1)
        self.assertTrue(body["power_risk_detected"])
        post.assert_not_called()

    def _get_airspace(
        self,
        is_did: bool | None,
        lat: float,
        lon: float,
        counts: tuple[int, int] = (0, 0),
    ):
        """DID 判定と OSM 取得をモックして空域判定だけを検証する。"""

        with patch(
            "legal.fetch_did_status", return_value=(is_did, "テスト用のDID判定")
        ), patch("main._fetch_osm_api_counts", return_value=counts), patch(
            "main.requests.post"
        ) as post:
            response = client.get(
                "/api/check-infrastructure",
                params={"lat": lat, "lon": lon, "radius": 300},
            )
        post.assert_not_called()
        return response

    def test_did_inside_upgrades_category_to_iib(self) -> None:
        response = self._get_airspace(True, LAT, LON)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["is_did"])
        self.assertEqual(body["did_source"], "gsi-did2020")
        self.assertTrue(body["is_specific_flight"])
        self.assertEqual(body["specific_flight_basis"], "legal")
        self.assertEqual(body["category_min"], "IIB")
        self.assertEqual([flag["key"] for flag in body["legal_flags"]], ["did"])
        self.assertEqual(body["safety_flags"], [])
        self.assertFalse(body["is_airport_vicinity"])
        self.assertIn("DID", body["message"])

    def test_rural_point_is_category_one(self) -> None:
        response = self._get_airspace(False, 43.66, 142.85)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["is_did"])
        self.assertFalse(body["is_specific_flight"])
        self.assertEqual(body["specific_flight_basis"], "none")
        self.assertEqual(body["category_min"], "I")
        self.assertEqual(body["legal_flags"], [])
        self.assertFalse(body["is_airport_vicinity"])
        self.assertIn("カテゴリーⅠ", body["message"])

    def test_airport_vicinity_is_category_iia(self) -> None:
        response = self._get_airspace(False, 35.5494, 139.7798)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["is_airport_vicinity"])
        self.assertEqual(body["airport"]["icao"], "RJTT")
        self.assertEqual([flag["key"] for flag in body["legal_flags"]], ["airport"])
        self.assertEqual(body["category_min"], "IIA")
        self.assertTrue(body["is_specific_flight"])
        self.assertIn("空港", body["message"])

    def test_unknown_did_is_reported_without_upgrade(self) -> None:
        response = self._get_airspace(None, 43.66, 142.85)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["is_did"])
        self.assertTrue(body["legal_unknown"])
        self.assertFalse(body["is_specific_flight"])
        self.assertEqual(body["category_min"], "I")
        self.assertIn("DIPS", body["message"])

    def test_power_risk_only_uses_safety_basis(self) -> None:
        response = self._get_airspace(False, 43.66, 142.85, counts=(1, 2))

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["legal_flags"], [])
        self.assertEqual([flag["key"] for flag in body["safety_flags"]], ["power"])
        self.assertTrue(body["is_specific_flight"])
        self.assertEqual(body["specific_flight_basis"], "safety")
        self.assertEqual(body["category_min"], "IIB")
        self.assertIn("安全上の要確認", body["message"])

    def test_did_fetch_failure_does_not_break_response(self) -> None:
        with patch(
            "legal.fetch_did_status", side_effect=RuntimeError("boom")
        ), patch("main._fetch_osm_api_counts", return_value=(0, 0)), patch(
            "main.requests.post"
        ):
            response = self._get()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["is_did"])

    def test_osm_failure_falls_back_to_overpass(self) -> None:
        with patch(
            "main.requests.get", side_effect=requests.ConnectionError("boom")
        ), patch(
            "main.requests.post",
            return_value=FakeResponse(b'{"elements": []}'),
        ) as post, patch.object(
            main, "_count_overpass_elements", return_value=(1, 1)
        ):
            response = self._get()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source"], "overpass")
        post.assert_called_once()

    def test_endpoint_is_async(self) -> None:
        """同期 def のままだとスレッドプールを占有して詰まるため、async であること。"""

        self.assertTrue(asyncio.iscoroutinefunction(main.check_infrastructure))

    def test_did_and_osm_run_in_parallel(self) -> None:
        """DID と OSM の取得が並列に走り、待ち時間が直列にならないこと。"""

        def slow_counts(*_args, **_kwargs) -> tuple[int, int, str]:
            time.sleep(0.4)
            return 0, 0, "osm-api"

        def slow_did(*_args, **_kwargs) -> tuple[bool, str]:
            time.sleep(0.4)
            return True, "テスト用のDID判定"

        with patch.object(
            main, "_fetch_infra_counts", side_effect=slow_counts
        ), patch("legal.fetch_did_status", side_effect=slow_did):
            response = self._get()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # 直列なら 800ms 以上。BFF 側の計測が 600ms 未満なら並列に走っている。
        self.assertLess(body["elapsed_ms"], 600, f"直列実行になっている: {body}")
        self.assertTrue(body["is_did"])

    def test_total_timeout_returns_safe_defaults(self) -> None:
        """全体の時間切れでは 504 ではなく「判定不能」の既定値を返すこと。"""

        def hanging_counts(*_args, **_kwargs) -> tuple[int, int, str]:
            time.sleep(2.0)
            return 0, 0, "osm-api"

        with patch.object(main, "INFRA_TOTAL_TIMEOUT_SECONDS", 0.5), patch.object(
            main, "INFRA_DEADLINE_SECONDS", 0.4
        ), patch.object(
            main, "_fetch_infra_counts", side_effect=hanging_counts
        ), patch(
            "legal.fetch_did_status", return_value=(None, "テスト用のDID判定")
        ):
            response = self._get()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # 応答自体は打ち切り時刻（0.5 秒）で返る。打ち切った取得スレッドは
        # requests の timeout で終わるまで残るが、レスポンスは待たない。
        self.assertLess(body["elapsed_ms"], 1500, f"打ち切りが効いていない: {body}")
        self.assertTrue(body["power_unknown"])
        self.assertFalse(body["power_risk_detected"])
        self.assertEqual(body["towers_count"], 0)
        self.assertEqual(body["lines_count"], 0)
        self.assertEqual(body["source"], "unknown")
        self.assertTrue(body["legal_unknown"])
        self.assertIsNone(body["is_did"])
        self.assertIn("DIPS", body["message"])

    def test_slow_did_does_not_block_infra_result(self) -> None:
        """DID が遅くても、インフラ結果は返し DID だけ判定不能にすること。"""

        def slow_did(*_args, **_kwargs) -> tuple[bool, str]:
            time.sleep(2.0)
            return True, "遅いDID判定"

        with patch.object(main, "INFRA_TOTAL_TIMEOUT_SECONDS", 0.8), patch.object(
            main, "INFRA_DEADLINE_SECONDS", 0.7
        ), patch.object(
            main, "_fetch_infra_counts", return_value=(2, 1, "osm-api")
        ), patch(
            "legal.fetch_did_status", side_effect=slow_did
        ):
            response = self._get()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["towers_count"], 2)
        self.assertEqual(body["lines_count"], 1)
        self.assertFalse(body["power_unknown"])
        self.assertIsNone(body["is_did"])
        self.assertTrue(body["legal_unknown"])

    def test_both_providers_fail(self) -> None:
        with patch(
            "main.requests.get", side_effect=requests.ConnectionError("osm down")
        ), patch(
            "main.requests.post", side_effect=requests.ConnectionError("overpass down")
        ):
            response = self._get()

        self.assertEqual(response.status_code, 502, response.text)
        self._assert_error(response.json(), "overpass_unavailable")

    def test_slow_body_is_cut_at_deadline(self) -> None:
        """細切れに届く応答でも、総時間の上限で打ち切って素早く失敗する。"""

        drip = FakeResponse(chunks=[b"x" * 1024] * 30, delay=0.2)
        with patch.object(main, "INFRA_DEADLINE_SECONDS", 0.6), patch(
            "main.requests.get", return_value=drip
        ), patch(
            "main.requests.post", side_effect=requests.ConnectionError("overpass down")
        ):
            started = time.monotonic()
            response = self._get()
            elapsed = time.monotonic() - started

        self.assertIn(response.status_code, (502, 504), response.text)
        self.assertLess(elapsed, 2.5, f"deadline を超えて待たされた: {elapsed:.2f}s")
        self.assertTrue(drip.closed, "応答を閉じていない")

    def test_read_body_raises_504_after_deadline(self) -> None:
        clock = {"now": 100.0}

        class DripResponse:
            status_code = 200

            def iter_content(self, chunk_size: int) -> Iterator[bytes]:
                for _ in range(10):
                    clock["now"] += 1.0
                    yield b"x"

            def close(self) -> None:
                pass

        with patch.object(main.time, "monotonic", side_effect=lambda: clock["now"]):
            with self.assertRaises(HTTPException) as ctx:
                main._read_body_with_deadline(DripResponse(), deadline=101.5)

        self.assertEqual(ctx.exception.status_code, 504)
        self.assertEqual(ctx.exception.detail["code"], "infra_deadline_exceeded")

    def test_bounded_timeout_uses_remaining_time(self) -> None:
        connect, read = main._bounded_timeout(time.monotonic() + 1.0, 3.5, 9.0)
        self.assertLessEqual(connect, 1.0)
        self.assertLessEqual(read, 1.0)
        self.assertGreater(connect, 0.0)

    def test_deadline_already_passed_still_allows_attempt(self) -> None:
        """残り時間が無くても 0.5 秒は確保し、即座に例外にはしない。"""

        connect, read = main._bounded_timeout(time.monotonic() - 10.0, 3.5, 9.0)
        self.assertEqual(connect, 0.5)
        self.assertEqual(read, 0.5)


if __name__ == "__main__":
    unittest.main()

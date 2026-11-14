"""`GET /api/check-infrastructure` のオフラインテスト。

OSM API / Overpass API へは接続せず、取得元の切り替えと「総処理時間の上限」
（応答が細切れに届いても deadline で打ち切る）を検証する。
実行: `.venv/bin/python tests/test_check_infrastructure.py -v`
"""

from __future__ import annotations

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

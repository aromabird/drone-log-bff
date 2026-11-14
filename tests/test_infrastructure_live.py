"""`GET /api/check-infrastructure` の実 API テスト（任意実行）。

OSM API と国土地理院タイルへ実際にアクセスするため、既定ではスキップする。
実行: `DRONE_LOG_LIVE=1 .venv/bin/python tests/test_infrastructure_live.py -v`
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)

LIVE = os.getenv("DRONE_LOG_LIVE") == "1"
WHY = "DRONE_LOG_LIVE=1 を指定したときだけ実行します（実 API へ接続）"

SHINAGAWA = {"lat": 35.6284, "lon": 139.7387}  # 品川駅（DID 内）
HANEDA = {"lat": 35.5494, "lon": 139.7798}  # 羽田空港の敷地内
DAISETSUZAN = {"lat": 43.66, "lon": 142.85}  # 大雪山（DID 外・空港から遠い）


@unittest.skipUnless(LIVE, WHY)
class LiveInfrastructureTests(unittest.TestCase):
    def _get(self, params: dict, radius: int = 300) -> dict:
        response = client.get(
            "/api/check-infrastructure", params={**params, "radius": radius}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_shinagawa_is_did_and_specific_flight(self) -> None:
        body = self._get(SHINAGAWA)

        self.assertTrue(body["is_did"], body)
        self.assertEqual(body["did_source"], "gsi-did2020")
        self.assertIn("did", [flag["key"] for flag in body["legal_flags"]])
        self.assertTrue(body["is_specific_flight"])
        self.assertEqual(body["specific_flight_basis"], "legal")
        self.assertEqual(body["category_min"], "IIB")
        self.assertLess(body["elapsed_ms"], 9000)

    def test_haneda_is_airport_vicinity(self) -> None:
        body = self._get(HANEDA)

        self.assertTrue(body["is_airport_vicinity"], body)
        self.assertEqual(body["airport"]["icao"], "RJTT")
        self.assertLessEqual(body["airport"]["distance_m"], 6000)
        self.assertEqual(body["category_min"], "IIA")

    def test_rural_point_is_category_one(self) -> None:
        body = self._get(DAISETSUZAN, radius=100)

        self.assertFalse(body["is_did"], body)
        self.assertFalse(body["is_airport_vicinity"])
        self.assertFalse(body["is_specific_flight"])
        self.assertEqual(body["category_min"], "I")
        self.assertFalse(body["legal_unknown"])


if __name__ == "__main__":
    unittest.main()

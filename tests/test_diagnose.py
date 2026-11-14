"""`POST /api/diagnose` のオフラインテスト。

Gemini API へは接続せず、HTTP 呼び出しと応答解析だけを検証する。
実行: `.venv/bin/python tests/test_diagnose.py -v`
"""

from __future__ import annotations

import base64
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

PNG_BASE64 = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
        "q842ZQAAAABJRU5ErkJggg=="
    )
).decode("ascii")

BASE_PAYLOAD = {
    "source": "conditions",
    "flight": {
        "date": "2026-11-14",
        "takeoff_place": "千葉県香取市小見川",
        "landing_place": "千葉県香取市小見川",
        "lat": 35.9,
        "lng": 140.5,
        "takeoff_time": "09:00",
        "landing_time": "09:20",
        "duration_min": 20,
        "purpose": "空撮",
        "specific_flags": ["DID", "150m"],
        "has_clear_zone": True,
    },
    "weather": {
        "text": "晴れ",
        "temperature": 18.5,
        "humidity": 55.0,
        "precipitation": 0.0,
        "wind_speed": 4.2,
        "wind_gust": 7.8,
        "safety_status": "CAUTION",
        "safety_label": "突風に注意",
    },
    "elevation": {
        "ground_elevation": 12.3,
        "max_150m_limit": 162.3,
        "note": "国土地理院の標高データ",
    },
    "regulations": {
        "is_did": True,
        "note": "人口集中地区（DID）内",
        "source": "国土地理院 地理院タイル（DID2020）",
    },
    "infrastructure": {
        "power_risk_detected": True,
        "towers_count": 2,
        "lines_count": 3,
        "message": "鉄塔2件・送電線3件",
        "source": "osm-api",
    },
    "drone": {
        "model_name": "DJI Mavic 3 Enterprise",
        "maker": "DJI",
        "weight_g": 920,
        "registration_no": "JU1234567890",
    },
    "pilot": {
        "license_type": "一等無人航空機操縦士",
        "license_endorsements": ["目視外飛行", "夜間飛行"],
    },
}


class FakeResponse:
    """requests.post の戻り値を模した最小オブジェクト。"""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def detail_code(response) -> str:
    return response.json().get("detail", {}).get("code", "")


class DiagnoseEndpointTest(unittest.TestCase):
    """正常系と入力検証。"""

    def test_conditions_success(self) -> None:
        captured: dict = {}

        def fake_gemini(prompt, image_base64, image_mime_type):
            captured["prompt"] = prompt
            captured["image"] = image_base64
            return "・総合判断: 飛行可能です。\n・突風7.8m/sに注意。"

        with patch.object(main, "GEMINI_API_KEY", "test-key"), \
                patch.object(main, "_request_gemini_text", side_effect=fake_gemini):
            response = client.post("/api/diagnose", json=BASE_PAYLOAD)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["source"], "conditions")
        self.assertEqual(body["model"], main.GEMINI_MODEL)
        self.assertIn("総合判断", body["diagnosis"])
        self.assertIn("generatedAt", body)
        self.assertEqual(captured["image"], "")
        self.assertIn("千葉県香取市小見川", captured["prompt"])
        self.assertIn("突風: 7.8m/s", captured["prompt"])
        self.assertIn("DID判定: DID内", captured["prompt"])
        self.assertIn("鉄塔数: 2件", captured["prompt"])
        self.assertIn("添付写真: なし", captured["prompt"])

    def test_photo_success_passes_inline_image(self) -> None:
        captured: dict = {}

        def fake_gemini(prompt, image_base64, image_mime_type):
            captured["prompt"] = prompt
            captured["image"] = image_base64
            captured["mime"] = image_mime_type
            return "・写真右上に電線。進入経路を変更してください。"

        payload = dict(BASE_PAYLOAD)
        payload["source"] = "photo"
        payload["image_base64"] = "data:image/png;base64," + PNG_BASE64
        payload["image_mime_type"] = "image/png"

        with patch.object(main, "GEMINI_API_KEY", "test-key"), \
                patch.object(main, "_request_gemini_text", side_effect=fake_gemini):
            response = client.post("/api/diagnose", json=payload)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source"], "photo")
        self.assertEqual(captured["image"], PNG_BASE64)
        self.assertEqual(captured["mime"], "image/png")
        self.assertIn("添付写真: 離陸地点周辺", captured["prompt"])

    def test_photo_requires_image(self) -> None:
        payload = dict(BASE_PAYLOAD)
        payload["source"] = "photo"
        with patch.object(main, "GEMINI_API_KEY", "test-key"):
            response = client.post("/api/diagnose", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(detail_code(response), "image_required")

    def test_rejects_unsupported_mime(self) -> None:
        payload = dict(BASE_PAYLOAD)
        payload["source"] = "photo"
        payload["image_base64"] = PNG_BASE64
        payload["image_mime_type"] = "image/gif"
        with patch.object(main, "GEMINI_API_KEY", "test-key"):
            response = client.post("/api/diagnose", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(detail_code(response), "invalid_image_type")

    def test_rejects_broken_base64(self) -> None:
        payload = dict(BASE_PAYLOAD)
        payload["source"] = "photo"
        payload["image_base64"] = "not-valid-base64!!"
        payload["image_mime_type"] = "image/jpeg"
        with patch.object(main, "GEMINI_API_KEY", "test-key"):
            response = client.post("/api/diagnose", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(detail_code(response), "invalid_image_data")

    def test_without_api_key_returns_503(self) -> None:
        with patch.object(main, "GEMINI_API_KEY", ""):
            response = client.post("/api/diagnose", json=BASE_PAYLOAD)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(detail_code(response), "ai_not_configured")

    def test_timeout_maps_to_504(self) -> None:
        with patch.object(main, "GEMINI_API_KEY", "test-key"), \
                patch.object(main.requests, "post", side_effect=main.requests.Timeout):
            response = client.post("/api/diagnose", json=BASE_PAYLOAD)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(detail_code(response), "ai_timeout")

    def test_rate_limit_maps_to_503(self) -> None:
        with patch.object(main, "GEMINI_API_KEY", "test-key"), \
                patch.object(main.requests, "post", return_value=FakeResponse(429)):
            response = client.post("/api/diagnose", json=BASE_PAYLOAD)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(detail_code(response), "ai_rate_limited")

    def test_empty_candidates_maps_to_502(self) -> None:
        with patch.object(main, "GEMINI_API_KEY", "test-key"), \
                patch.object(main.requests, "post", return_value=FakeResponse(200, {})):
            response = client.post("/api/diagnose", json=BASE_PAYLOAD)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(detail_code(response), "ai_empty_response")

    def test_success_payload_from_gemini(self) -> None:
        payload = {
            "candidates": [
                {"content": {"parts": [{"text": "・OK"}, {"text": "です。"}]}}
            ]
        }
        with patch.object(main, "GEMINI_API_KEY", "test-key"), \
                patch.object(main.requests, "post", return_value=FakeResponse(200, payload)):
            response = client.post("/api/diagnose", json=BASE_PAYLOAD)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["diagnosis"], "・OKです。")

    def test_safety_block_returns_422(self) -> None:
        payload = {"promptFeedback": {"blockReason": "SAFETY"}}
        with patch.object(main, "GEMINI_API_KEY", "test-key"), \
                patch.object(main.requests, "post", return_value=FakeResponse(200, payload)):
            response = client.post("/api/diagnose", json=BASE_PAYLOAD)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(detail_code(response), "ai_blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)





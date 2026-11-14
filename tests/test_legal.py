"""`legal.py`（DID・空港周辺の判定）のオフラインテスト。

ネットワークへは接続せず、PNG デコード・タイル座標変換・空港距離・
カテゴリー判定を検証する。
実行: `.venv/bin/python tests/test_legal.py -v`
"""

from __future__ import annotations

import struct
import sys
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

import legal  # noqa: E402

SHINAGAWA = (35.6284, 139.7387)
HANEDA = (35.5494, 139.7798)
DAISETSUZAN = (43.66, 142.85)


def _paeth(left: int, up: int, up_left: int) -> int:
    p = left + up - up_left
    pa = abs(p - left)
    pb = abs(p - up)
    pc = abs(p - up_left)
    if pa <= pb and pa <= pc:
        return left
    if pb <= pc:
        return up
    return up_left


def _filter_row(row: bytes, prev: bytes, filter_type: int) -> bytes:
    """生のピクセル列を PNG のフィルタ形式へ変換する（デコーダの逆）。"""

    bpp = 4
    out = bytearray(len(row))
    for i, value in enumerate(row):
        left = row[i - bpp] if i >= bpp else 0
        up = prev[i]
        up_left = prev[i - bpp] if i >= bpp else 0
        if filter_type == 0:
            predictor = 0
        elif filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = up
        elif filter_type == 3:
            predictor = (left + up) >> 1
        else:
            predictor = _paeth(left, up, up_left)
        out[i] = (value - predictor) & 0xFF
    return bytes(out)


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", crc)
    )


def make_png(width: int, height: int, alpha_rows: list[list[int]]) -> bytes:
    """各行のアルファ値から 8bit RGBA PNG を組み立てる（RGB は固定値）。"""

    raw = bytearray()
    prev = bytes(width * 4)
    for y in range(height):
        row = bytearray()
        for alpha in alpha_rows[y]:
            row.extend((0x22, 0x44, 0x66, alpha))
        filter_type = y % 5  # 0〜4 のフィルタを一通り通す
        raw.append(filter_type)
        raw.extend(_filter_row(bytes(row), prev, filter_type))
        prev = bytes(row)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        legal.PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _chunk(b"IEND", b"")
    )


class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


class TileMathTests(unittest.TestCase):
    def test_shinagawa_tile(self) -> None:
        # 品川駅は z16 の x=58206 / y=25818 タイル内（実タイルで確認済み）。
        tile_x, tile_y, px, py = legal.latlon_to_tile(*SHINAGAWA)
        self.assertEqual((tile_x, tile_y), (58206, 25818))
        self.assertEqual((px, py), (167, 121))

    def test_pixel_is_within_tile(self) -> None:
        for lat, lon in (SHINAGAWA, HANEDA, DAISETSUZAN, (0.0, 0.0), (-33.9, 151.2)):
            _, _, px, py = legal.latlon_to_tile(lat, lon)
            self.assertTrue(0 <= px < 256 and 0 <= py < 256, (lat, lon, px, py))


class PngDecodeTests(unittest.TestCase):
    def test_alpha_of_each_pixel(self) -> None:
        alphas = [[(x * 7 + y * 3) % 256 for x in range(5)] for y in range(5)]
        png = make_png(5, 5, alphas)
        for y in range(5):
            for x in range(5):
                self.assertEqual(
                    legal.decode_png_pixel_alpha(png, x, y), alphas[y][x], (x, y)
                )

    def test_rejects_broken_png(self) -> None:
        with self.assertRaises(ValueError):
            legal.decode_png_pixel_alpha(b"not a png", 0, 0)

    def test_rejects_out_of_range_pixel(self) -> None:
        png = make_png(2, 2, [[0, 0], [0, 0]])
        with self.assertRaises(ValueError):
            legal.decode_png_pixel_alpha(png, 2, 0)


class DidFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        legal.reset_caches()

    def test_404_means_outside_did(self) -> None:
        with patch("legal.requests.get", return_value=FakeResponse(404)) as get:
            is_did, note = legal.fetch_did_status(*SHINAGAWA)

        self.assertIs(is_did, False)
        self.assertIn("人口集中地区外", note)
        self.assertEqual(get.call_count, 1)

    def test_alpha_hit_means_inside_did(self) -> None:
        _, _, px, py = legal.latlon_to_tile(*SHINAGAWA)
        alphas = [[0] * 256 for _ in range(256)]
        alphas[py][px] = 255
        with patch(
            "legal.requests.get",
            return_value=FakeResponse(200, make_png(256, 256, alphas)),
        ):
            is_did, note = legal.fetch_did_status(*SHINAGAWA)

        self.assertIs(is_did, True)
        self.assertIn("人口集中地区内", note)

    def test_transparent_pixel_means_outside_did(self) -> None:
        with patch(
            "legal.requests.get",
            return_value=FakeResponse(200, make_png(256, 256, [[0] * 256] * 256)),
        ):
            is_did, _ = legal.fetch_did_status(*SHINAGAWA)

        self.assertIs(is_did, False)

    def test_network_error_is_unknown(self) -> None:
        with patch("legal.requests.get", side_effect=requests.Timeout("slow")):
            is_did, note = legal.fetch_did_status(*SHINAGAWA, deadline=None)

        self.assertIsNone(is_did)
        self.assertIn("判定できませんでした", note)

    def test_unexpected_status_is_unknown(self) -> None:
        with patch("legal.requests.get", return_value=FakeResponse(503)):
            is_did, _ = legal.fetch_did_status(*SHINAGAWA)

        self.assertIsNone(is_did)

    def test_result_is_cached(self) -> None:
        with patch("legal.requests.get", return_value=FakeResponse(404)) as get:
            legal.fetch_did_status(*SHINAGAWA)
            legal.fetch_did_status(*SHINAGAWA)

        self.assertEqual(get.call_count, 1)

    def test_deadline_shortens_timeout(self) -> None:
        with patch("legal.requests.get", return_value=FakeResponse(404)) as get:
            legal.fetch_did_status(*SHINAGAWA, deadline=time.monotonic() + 0.2)

        timeout = get.call_args.kwargs["timeout"]
        self.assertLessEqual(timeout[0], 0.5)
        self.assertLessEqual(timeout[1], 0.5)



class AirportTests(unittest.TestCase):
    def setUp(self) -> None:
        legal.reset_caches()

    def test_dataset_is_bundled(self) -> None:
        airports = legal.load_airports()
        self.assertGreater(len(airports), 80)
        self.assertTrue(all(isinstance(a["lat"], float) for a in airports))

    def test_haneda_is_vicinity(self) -> None:
        airport = legal.nearest_airport(*HANEDA)
        self.assertIsNotNone(airport)
        self.assertEqual(airport["icao"], "RJTT")
        self.assertTrue(airport["vicinity"])
        self.assertLess(airport["distance_m"], 1500)

    def test_shinagawa_is_near_but_not_vicinity(self) -> None:
        airport = legal.nearest_airport(*SHINAGAWA)
        self.assertEqual(airport["icao"], "RJTT")
        self.assertGreater(airport["distance_m"], airport["radius_m"])
        self.assertFalse(airport["vicinity"])
        self.assertTrue(airport["nearby"])

    def test_daisetsuzan_is_far_from_airports(self) -> None:
        airport = legal.nearest_airport(*DAISETSUZAN)
        if airport is not None:
            self.assertFalse(airport["vicinity"])
            self.assertGreater(airport["distance_m"], 12000)


class JudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        legal.reset_caches()

    def test_did_only_is_category_iib(self) -> None:
        airspace = legal.judge_airspace(*SHINAGAWA, is_did=True)
        self.assertEqual([f["key"] for f in airspace["legal_flags"]], ["did"])
        self.assertFalse(airspace["is_airport_vicinity"])
        self.assertFalse(airspace["legal_unknown"])
        code, label, basis = legal.build_category(airspace["legal_flags"], [])
        self.assertEqual((code, basis), ("IIB", "legal"))
        self.assertIn("ⅡB", label)

    def test_airport_wins_over_did(self) -> None:
        airspace = legal.judge_airspace(*HANEDA, is_did=True)
        self.assertEqual(
            [f["key"] for f in airspace["legal_flags"]], ["did", "airport"]
        )
        code, _, basis = legal.build_category(airspace["legal_flags"], [])
        self.assertEqual((code, basis), ("IIA", "legal"))

    def test_no_flags_is_category_one(self) -> None:
        airspace = legal.judge_airspace(*DAISETSUZAN, is_did=False)
        self.assertEqual(airspace["legal_flags"], [])
        code, label, basis = legal.build_category([], [])
        self.assertEqual((code, basis), ("I", "none"))
        self.assertEqual(label, "カテゴリーⅠ")

    def test_power_risk_is_safety_basis(self) -> None:
        flags = legal.power_safety_flag(2, 1)
        self.assertEqual(flags[0]["key"], "power")
        self.assertIn("鉄塔2基", flags[0]["detail"])
        code, _, basis = legal.build_category([], flags)
        self.assertEqual((code, basis), ("IIB", "safety"))

    def test_power_safety_flag_is_empty_without_risk(self) -> None:
        self.assertEqual(legal.power_safety_flag(0, 0), [])

    def test_unknown_did_is_flagged(self) -> None:
        airspace = legal.judge_airspace(*DAISETSUZAN, is_did=None)
        self.assertTrue(airspace["legal_unknown"])
        self.assertEqual(airspace["legal_flags"], [])


if __name__ == "__main__":
    unittest.main()


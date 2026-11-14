"""ドローン飛行の空域判定（人口集中地区・空港等の周辺）。

PWA の「周辺インフラのリスクチェック」に対し、航空法上の特定飛行に該当する
空域かどうかの**参考判定**を返すモジュール。外部依存を増やさないため、画像の
デコードは標準ライブラリ（`zlib` / `struct`）だけで行う。

判定の根拠:

- DID（人口集中地区）: 国土地理院の DID2020 タイル（ズーム16・PNG/alpha）を
  サーバー側で取得し、対象ピクセルの不透明度で判定する。タイルが 404 の場合は
  「そのタイルに DID が無い」ことを意味する（地理院タイルの仕様）。
  出典: 国土地理院「人口集中地区（DID2020）」
  https://cyberjapandata.gsi.go.jp/xyz/did2020/
- 空港等の周辺: 航空法上の「空港等の周辺空域」は進入表面・転移表面などの
  表面で定義され、**半径の数値規定はない**（国土交通省の解説参照）。
  本モジュールは実務上の目安として、空港の標点から 6km 以内を
  「該当（目安）」、12km 以内を「近接（要確認）」として扱う。
  最終判断は DIPS / 地理院地図で行うこと。
  https://www.mlit.go.jp/koku/koku_fr10_000041.html
"""

from __future__ import annotations

import json
import logging
import math
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("drone-log-bff.legal")

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
AIRPORTS_PATH = ASSETS_DIR / "airports_jp.json"

# 国土地理院 人口集中地区（DID2020）タイル。API キーは不要。
GSI_DID_TILE_URL = "https://cyberjapandata.gsi.go.jp/xyz/did2020/{z}/{x}/{y}.png"
DID_ZOOM = 16
DID_TILE_SIZE = 256
# 接続1.5秒・応答2.0秒。インフラ取得（9秒）と並列に走らせるため短く抑える。
DID_FETCH_TIMEOUT = (1.5, 2.0)
DID_SOURCE = "gsi-did2020"
DID_CACHE_TTL_SECONDS = 24 * 60 * 60
DID_CACHE_MAX_ENTRIES = 512

# 空港判定のしきい値（目安）。法令上の境界は進入表面等で決まる。
AIRPORT_VICINITY_RADIUS_M = 6000
AIRPORT_NEARBY_RADIUS_M = 12000

DID_FLAG_LABEL = "DID（人口集中地区）"
AIRPORT_FLAG_LABEL = "空港周辺"

DID_NOTE_IN = "国土地理院 DID2020（2020年国勢調査）: 人口集中地区内"
DID_NOTE_OUT = "国土地理院 DID2020（2020年国勢調査）: 人口集中地区外"
DID_NOTE_UNKNOWN = "DID を判定できませんでした（国土地理院タイルを取得できません）"
DID_NOTE_TIMEOUT = "DID の判定が時間内に完了しませんでした（DIPS で確認してください）"

CATEGORY_LABELS = {
    "I": "カテゴリーⅠ",
    "IIA": "カテゴリーⅡA",
    "IIB": "カテゴリーⅡB",
    "III": "カテゴリーⅢ",
}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
USER_AGENT = "drone-log-bff/1.0 (drone preflight check)"

EARTH_RADIUS_M = 6371000.0

# DID タイルのピクセル単位キャッシュ（同じ座標の再チェックを高速化する）。
_DID_CACHE: dict[tuple[int, int, int, int, int], tuple[float, bool]] = {}
_DID_CACHE_LOCK = threading.Lock()

_AIRPORTS: list[dict[str, Any]] | None = None
_AIRPORTS_LOCK = threading.Lock()
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の大円距離（メートル）。"""

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def latlon_to_tile(
    lat: float, lon: float, zoom: int = DID_ZOOM
) -> tuple[int, int, int, int]:
    """緯度経度から (tile_x, tile_y, pixel_x, pixel_y) を返す（Web メルカトル）。"""

    scale = 2**zoom
    x = (lon + 180.0) / 360.0 * scale
    rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * scale
    tile_x = int(math.floor(x))
    tile_y = int(math.floor(y))
    pixel_x = min(DID_TILE_SIZE - 1, max(0, int((x - tile_x) * DID_TILE_SIZE)))
    pixel_y = min(DID_TILE_SIZE - 1, max(0, int((y - tile_y) * DID_TILE_SIZE)))
    return tile_x, tile_y, pixel_x, pixel_y


def decode_png_pixel_alpha(data: bytes, px: int, py: int) -> int:
    """8bit RGBA（color type 6）PNG の指定ピクセルのアルファ値を返す。

    地理院タイルは 256x256・8bit RGBA の PNG。フィルタ復元（0〜4）だけを
    実装した最小のデコーダで、外部ライブラリを必要としない。
    """

    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("PNG シグネチャが不正です")

    pos = len(PNG_SIGNATURE)
    width = height = bit_depth = color_type = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
            if bit_depth != 8 or color_type != 6:
                raise ValueError(
                    "未対応の PNG 形式です "
                    f"(bit_depth={bit_depth}, color_type={color_type})"
                )
        elif chunk_type == b"IDAT":
            idat.extend(chunk)
        elif chunk_type == b"IEND":
            break

    if not idat or width <= 0 or height <= 0:
        raise ValueError("PNG の IHDR / IDAT がありません")
    if not (0 <= px < width and 0 <= py < height):
        raise ValueError("ピクセル座標が画像の範囲外です")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:  # pragma: no cover - 壊れた入力は呼び出し側で握る
        raise ValueError(f"PNG の IDAT を展開できません: {exc}") from exc

    bpp = 4
    stride = width * bpp
    prev = bytearray(stride)
    offset = 0
    row = bytearray(stride)

    for _ in range(py + 1):
        if offset >= len(raw):
            raise ValueError("PNG の画素データが不足しています")
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset : offset + stride])
        offset += stride
        if len(row) < stride:
            raise ValueError("PNG の画素データが不足しています")

        if filter_type == 0:
            pass
        elif filter_type == 1:  # Sub
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                up = prev[i]
                up_left = prev[i - bpp] if i >= bpp else 0
                p = left + up - up_left
                pa = abs(p - left)
                pb = abs(p - up)
                pc = abs(p - up_left)
                if pa <= pb and pa <= pc:
                    predictor = left
                elif pb <= pc:
                    predictor = up
                else:
                    predictor = up_left
                row[i] = (row[i] + predictor) & 0xFF
        else:
            raise ValueError(f"未対応の PNG フィルタです: {filter_type}")

        prev = row

    return row[px * bpp + 3]

def _cache_get(key: tuple[int, int, int, int, int]) -> bool | None:
    now = time.monotonic()
    with _DID_CACHE_LOCK:
        entry = _DID_CACHE.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < now:
            _DID_CACHE.pop(key, None)
            return None
        return value


def _cache_put(key: tuple[int, int, int, int, int], value: bool) -> None:
    now = time.monotonic()
    with _DID_CACHE_LOCK:
        if len(_DID_CACHE) >= DID_CACHE_MAX_ENTRIES:
            expired = [k for k, (exp, _) in _DID_CACHE.items() if exp < now]
            for stale in expired:
                _DID_CACHE.pop(stale, None)
            if len(_DID_CACHE) >= DID_CACHE_MAX_ENTRIES:
                _DID_CACHE.clear()
        _DID_CACHE[key] = (now + DID_CACHE_TTL_SECONDS, value)


def reset_caches() -> None:
    """テスト用: DID キャッシュと空港データの読み込み状態を初期化する。"""

    global _AIRPORTS
    with _DID_CACHE_LOCK:
        _DID_CACHE.clear()
    with _AIRPORTS_LOCK:
        _AIRPORTS = None


def _timeout_for(deadline: float | None) -> tuple[float, float]:
    connect, read = DID_FETCH_TIMEOUT
    if deadline is None:
        return (connect, read)
    remaining = max(0.5, deadline - time.monotonic())
    return (min(connect, remaining), min(read, remaining))


def fetch_did_status(
    lat: float, lon: float, deadline: float | None = None
) -> tuple[bool | None, str]:
    """指定座標が DID（人口集中地区）内かを返す。

    Returns:
        (True / False / None, 表示用の補足)。None は判定不能（取得失敗・
        タイムアウト・想定外の画像形式）。
    """

    tile_x, tile_y, pixel_x, pixel_y = latlon_to_tile(lat, lon)
    cache_key = (DID_ZOOM, tile_x, tile_y, pixel_x, pixel_y)
    cached = _cache_get(cache_key)
    if cached is True:
        return True, DID_NOTE_IN
    if cached is False:
        return False, DID_NOTE_OUT

    url = GSI_DID_TILE_URL.format(z=DID_ZOOM, x=tile_x, y=tile_y)
    try:
        response = requests.get(
            url,
            timeout=_timeout_for(deadline),
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:
        logger.warning("DID tile fetch failed: %s (%s)", url, exc)
        return None, DID_NOTE_UNKNOWN

    if response.status_code == 404:
        # 地理院タイルは「そのタイルに DID が無い」場合に 404 を返す。
        _cache_put(cache_key, False)
        return False, DID_NOTE_OUT
    if response.status_code != 200:
        logger.warning("DID tile returned status=%s url=%s", response.status_code, url)
        return None, DID_NOTE_UNKNOWN

    try:
        alpha = decode_png_pixel_alpha(response.content, pixel_x, pixel_y)
    except ValueError as exc:
        logger.warning("DID tile decode failed: %s (%s)", url, exc)
        return None, DID_NOTE_UNKNOWN

    is_did = alpha > 0
    _cache_put(cache_key, is_did)
    return is_did, DID_NOTE_IN if is_did else DID_NOTE_OUT

def load_airports() -> list[dict[str, Any]]:
    """`assets/airports_jp.json` を読み込む（プロセス内で1回だけ）。"""

    global _AIRPORTS
    if _AIRPORTS is not None:
        return _AIRPORTS
    with _AIRPORTS_LOCK:
        if _AIRPORTS is not None:
            return _AIRPORTS
        try:
            payload = json.loads(AIRPORTS_PATH.read_text(encoding="utf-8"))
            airports = payload.get("airports") or []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("airport dataset unavailable: %s (%s)", AIRPORTS_PATH, exc)
            airports = []
        _AIRPORTS = [
            airport
            for airport in airports
            if isinstance(airport, dict)
            and isinstance(airport.get("lat"), (int, float))
            and isinstance(airport.get("lon"), (int, float))
        ]
        return _AIRPORTS


def nearest_airport(lat: float, lon: float) -> dict[str, Any] | None:
    """最寄りの空港と距離（m）を返す。データが無ければ None。"""

    airports = load_airports()
    if not airports:
        return None

    best: dict[str, Any] | None = None
    best_distance = float("inf")
    for airport in airports:
        # 緯度差で粗く絞ってから距離を計算する（108件なので十分速い）。
        if abs(airport["lat"] - lat) > 0.5:
            continue
        distance = haversine_m(lat, lon, airport["lat"], airport["lon"])
        if distance < best_distance:
            best_distance = distance
            best = airport

    if best is None:
        return None

    distance_m = int(round(best_distance))
    return {
        "name": best.get("name") or "名称不明の空港",
        "icao": best.get("icao") or "",
        "iata": best.get("iata") or "",
        "distance_m": distance_m,
        "radius_m": AIRPORT_VICINITY_RADIUS_M,
        "vicinity": distance_m <= AIRPORT_VICINITY_RADIUS_M,
        "nearby": distance_m <= AIRPORT_NEARBY_RADIUS_M,
    }


def judge_airspace(lat: float, lon: float, is_did: bool | None) -> dict[str, Any]:
    """DID と空港の判定結果から、法定フラグと理由を組み立てる。"""

    airport = nearest_airport(lat, lon)
    is_airport_vicinity = bool(airport and airport["vicinity"])

    legal_flags: list[dict[str, str]] = []
    reasons: list[str] = []

    if is_did is True:
        legal_flags.append(
            {"key": "did", "label": DID_FLAG_LABEL, "detail": DID_NOTE_IN}
        )
        reasons.append("人口集中地区（DID）の上空")
    if is_airport_vicinity and airport is not None:
        legal_flags.append(
            {
                "key": "airport",
                "label": AIRPORT_FLAG_LABEL,
                "detail": f"{airport['name']}から{airport['distance_m'] / 1000:.1f}km",
            }
        )
        reasons.append("空港等の周辺空域（6km以内の目安）")

    return {
        "is_airport_vicinity": is_airport_vicinity,
        "airport": airport,
        "legal_flags": legal_flags,
        "category_reasons": reasons,
        "legal_unknown": is_did is None,
    }


def power_safety_flag(towers_count: int, lines_count: int) -> list[dict[str, str]]:
    """物理インフラ（送電線・鉄塔）の検知を安全フラグとして返す。

    送電線・鉄塔は航空法上の特定飛行10項目には含まれないため、法定フラグ
    （`legal_flags`）とは分けて扱う。
    """

    if towers_count <= 0 and lines_count <= 0:
        return []
    return [
        {
            "key": "power",
            "label": "送電線・鉄塔",
            "detail": f"鉄塔{towers_count}基 / 送電線{lines_count}件",
        }
    ]


def build_category(
    legal_flags: list[dict[str, str]], safety_flags: list[dict[str, str]]
) -> tuple[str, str, str]:
    """特定飛行の下限カテゴリーを返す。

    Returns:
        (コード, ラベル, 根拠)。根拠は "legal"（法令上の特定飛行）/
        "safety"（安全上の要確認）/ "none"（該当なし）。

    Note:
        最終的なカテゴリーは機体重量と立入管理措置でも変わるため、PWA 側の
        `judgeCategoryLocal()` が正となる。ここでは空域だけで決まる下限を返す。
    """

    keys = {flag["key"] for flag in legal_flags}
    if "airport" in keys:
        return "IIA", CATEGORY_LABELS["IIA"], "legal"
    if legal_flags:
        return "IIB", CATEGORY_LABELS["IIB"], "legal"
    if safety_flags:
        return "IIB", CATEGORY_LABELS["IIB"], "safety"
    return "I", CATEGORY_LABELS["I"], "none"


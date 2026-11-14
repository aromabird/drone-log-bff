"""drone-log PWA 用 BFF.

`POST /api/parse-log` で DJI の `.txt` ログをバイナリ解析し、PWA が使う
日付・離着陸時刻（JST）・飛行時間・離陸地点を返す。`POST /api/export-log` では
様式1の Excel テンプレートへ入力を書き込み、オンメモリで `.xlsx` を返す。
`GET /api/check-infrastructure` では OSM API / Overpass API から周辺の送電線・
鉄塔を取得する（小半径は応答の速い OSM API を優先し、失敗時のみ Overpass）。

解析には `pydjirecord` を使う。DJI の内部時刻は UTC で記録されるため、
レスポンスは必ず JST（UTC+9）へ変換する。v13 以降のフレーム（GPS 軌跡）は
DJI の API キーが必要だが、Details（日時・座標・飛行時間）はキーなしでも
復号できる。API キーは環境変数 `DJI_API_KEY` から読み、未設定時は Details を
使う。アップロードされた内容はメモリ上で読み捨て、ディスクへ保存しない。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from pydjirecord import DJILog

SERVICE_NAME = "drone-log-bff"

# 日本標準時（UTC+9）。日本には夏時間がないため固定オフセットでよい。
JST = timezone(timedelta(hours=9), name="JST")

# 異常値を弾くための下限・上限。DJI の飛行ログは通常この範囲に収まる。
MIN_LOG_YEAR = 2000
MAX_FLIGHT_SECONDS = 24 * 60 * 60

# 国交省様式1の飛行日誌テンプレート。セル番地はテンプレート側のレイアウトに
# 合わせてあり、変更する場合は README の対応表も更新すること。
TEMPLATE_PATH = Path(__file__).resolve().parent / "template" / "flight_record_template.xlsx"
EXCEL_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

# PWA の localStorage バックアップ。data/ は .gitignore 対象で、ファイルには
# 操縦者名・技能証明番号・連絡先などの個人情報が含まれるため 0600 で保存する。
# テスト時に保存先を切り替えられるよう環境変数で上書きできる。
DATA_DIR = Path(
    os.getenv(
        "DRONE_LOG_DATA_DIR",
        str(Path(__file__).resolve().parent / "data"),
    )
).expanduser()
BACKUP_PATH = DATA_DIR / "backup.json"
BACKUP_TMP_PATH = DATA_DIR / "backup.json.tmp"
MAX_BACKUP_BYTES = 10 * 1024 * 1024
BACKUP_LOCK = asyncio.Lock()

# 周辺インフラ（送電線・鉄塔）の取得設定。
#
# Overpass API は混雑・クォータ超過（406/429）で無応答になることが多く、
# 待たされる割に成功率が低い。そのため既定では「小さい半径なら OSM API を
# 先に試す」順序にしている（OSM API は 300m 程度の bbox なら数秒で返る）。
# radius が OSM_API_PREFERRED_MAX_RADIUS を超えるときだけ Overpass を先に試す。
OVERPASS_API_URLS = [
    url.strip()
    for url in (
        os.getenv("OVERPASS_API_URLS")
        or os.getenv("OVERPASS_API_URL")
        or "https://overpass-api.de/api/interpreter"
    ).split(",")
    if url.strip()
]
# タイムアウトしたミラーを待ち続けないよう、接続3秒・応答6秒に抑える。
# timeout はリトライしない（待ち時間が倍になるだけで成功率が上がらないため）。
OVERPASS_REQUEST_TIMEOUT = (3.0, 6.0)
OVERPASS_TRANSIENT_STATUS = frozenset({406, 429, 502, 503, 504})
# Overpass が落ちていると判定したら、この秒数だけ Overpass を試さない。
OVERPASS_COOLDOWN_SECONDS = 300.0
_OVERPASS_DOWN_UNTIL = 0.0

# これ以下の半径は OSM API を優先する（bbox が小さく 50k node 制限に掛からない）。
OSM_API_PREFERRED_MAX_RADIUS = 1000

# 1リクエストあたりの総処理時間の上限。超過したら 504 を返して PWA に委ねる。
INFRA_DEADLINE_SECONDS = 15.0

# Overpass が混雑・障害のときだけ使う公式 OSM API のフォールバック。
# bbox 内の全要素を取得し、BFF 側で半径内の鉄塔・送電線を数える。
OSM_API_URL = os.getenv(
    "OSM_API_URL", "https://api.openstreetmap.org/api/0.6/map"
).strip()
OSM_API_TIMEOUT = (3.5, 9.0)

logger = logging.getLogger(SERVICE_NAME)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(
    title="Drone Log BFF",
    description=(
        "ドローン飛行日誌 PWA のバックエンド。"
        "DJI の .txt ログをバイナリ解析し、JST の離着陸時刻・飛行時間・"
        "離陸地点を返します。飛行日誌の Excel（様式1）も生成できます。"
        "周辺の送電線・鉄塔は Overpass API から取得します。"
    ),
    version="0.4.0",
)

# CORS は「PWA の配信元」と「ローカル開発」だけに限定する。
# PWA は cookie を使わないため credentials は無効（allow_origins=["*"] にしない）。
CORS_ALLOW_ORIGINS = [
    "https://aromabird.github.io",
    "https://mac-mini-m4.tail95f7a2.ts.net",
]
# localhost / 127.0.0.1 / [::1] と Tailscale の CGNAT 帯 (100.64.0.0/10) は
# ポート番号を問わず許可する（`python3 -m http.server` の任意ポートに対応）。
CORS_ALLOW_ORIGIN_REGEX = (
    r"^https?://(?:"
    r"localhost|127\.0\.0\.1|\[::1\]|"
    r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
    r")(?::\d+)?$"
)

# 本番の bind は launchd（com.aromabird.drone-log-bff.plist）が
# `--host 0.0.0.0 --port 8000` を指定する。Tailscale の HTTPS は
# `tailscale serve` が 443 → http://127.0.0.1:8000 へプロキシする。
# 認証は無いため、Tailscale Funnel（一般公開）は使わないこと。
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
    # Chrome の Private Network Access (PNA)。公開 HTTPS の PWA から
    # Tailscale の 100.64.0.0/10 へアクセスするために必要。
    allow_private_network=True,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=600,
    # PWA がダウンロード時のファイル名を参照できるようにする。
    expose_headers=["Content-Disposition"],
)

Coordinates = tuple[float, float]


class InfrastructureRisk(BaseModel):
    """周辺インフラ（送電線・鉄塔）のチェック結果。"""

    power_risk_detected: bool = Field(
        ..., description="鉄塔または送電線が1つ以上あれば true"
    )
    towers_count: int = Field(..., ge=0, description="検索範囲内の鉄塔数")
    lines_count: int = Field(..., ge=0, description="検索範囲内の送電線数")
    message: str = Field(..., description="PWA に表示する判定メッセージ")
    source: str = Field(
        "unknown", description="データ取得元（osm-api / overpass / unknown）"
    )
    elapsed_ms: int = Field(0, ge=0, description="BFF 側の処理時間（ミリ秒）")


class FlightRecord(BaseModel):
    """飛行日誌（Excel）へ書き出す 1 飛行分の入力。"""

    date: str = Field(..., max_length=32, description="飛行年月日（YYYY-MM-DD）")
    operator_name: str = Field(..., max_length=120, description="操縦者氏名")
    registration_id: str = Field(..., max_length=64, description="機体登録記号")
    purpose: str = Field(..., max_length=120, description="飛行目的")
    location: str = Field(..., max_length=240, description="離陸場所")
    takeoff_time: str = Field(..., max_length=16, description="離陸時刻（HH:MM）")
    landing_time: str = Field(..., max_length=16, description="着陸時刻（HH:MM）")
    total_time: str = Field(..., max_length=32, description="飛行時間（表示用）")
    safety_measures: str = Field("", max_length=500, description="安全対策")
    notes: str = Field("", max_length=1000, description="備考")


@dataclass(frozen=True)
class FlightSummary:
    """DJI ログから抽出した 1 飛行分のサマリー（すべて表示用に整形済み）。"""

    date: str
    takeoff_time: str
    landing_time: str
    duration_minutes: int
    lat: float
    lng: float
    takeoff_place: str


def safe_filename(filename: str | None) -> str:
    """アップロードされたファイル名からディレクトリ部分を除く。"""
    if not filename:
        return ""
    # POSIX / Windows のどちらの区切り文字にも対応する。
    return filename.replace("\\", "/").rsplit("/", 1)[-1].strip()


def _valid_coordinate(lat: Any, lng: Any) -> bool:
    """緯度経度として妥当か判定する。

    DJI は GPS 未取得時に `0, 0` を入れることがあるため、両方 0 の座標は
    「有効な GPS 座標なし」として扱う。
    """

    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return False

    if not (math.isfinite(lat_f) and math.isfinite(lng_f)):
        return False
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
        return False
    return not (abs(lat_f) < 1e-9 and abs(lng_f) < 1e-9)


def _first_valid_gps(frames: list[Any]) -> Coordinates | None:
    """フレーム列から最初の有効な GPS 座標を返す。

    pydjirecord の FrameDetails と同じ基準（GPS 使用中かつ gps_level >= 3）で
    判定する。フレームが空、または有効な座標がない場合は None。
    """

    for frame in frames:
        osd = getattr(frame, "osd", None)
        if osd is None:
            continue
        if not bool(getattr(osd, "is_gpd_used", False)):
            continue
        try:
            gps_level = int(getattr(osd, "gps_level", 0))
        except (TypeError, ValueError):
            continue
        if gps_level < 3:
            continue

        lat = getattr(osd, "latitude", None)
        lng = getattr(osd, "longitude", None)
        if _valid_coordinate(lat, lng):
            return float(lat), float(lng)

    return None


def _load_frames(log: DJILog) -> list[Any]:
    """フレームを復号する。失敗しても Details で継続できるため例外は握る。"""

    version = int(getattr(log, "version", 0))

    if version >= 13:
        api_key = os.getenv("DJI_API_KEY", "").strip()
        if not api_key:
            logger.info("v13+ log: DJI_API_KEY is not set; using Details only")
            return []
        try:
            keychains = log.fetch_keychains(api_key)
            return log.frames(keychains)
        except Exception as exc:  # noqa: BLE001 - 解析失敗は Details へフォールバック
            logger.warning(
                "v13+ frame decode failed (%s); using Details only",
                type(exc).__name__,
            )
            return []

    try:
        return log.frames()
    except Exception as exc:  # noqa: BLE001 - 解析失敗は Details へフォールバック
        logger.warning(
            "frame decode failed (%s); using Details only",
            type(exc).__name__,
        )
        return []


def _seconds_from_frames(frames: list[Any]) -> float:
    """Details.total_time が 0 のとき、フレームの fly_time から飛行秒数を補う。"""

    fly_times: list[float] = []
    for frame in frames:
        osd = getattr(frame, "osd", None)
        if osd is None:
            continue
        try:
            fly_time = float(getattr(osd, "fly_time", 0.0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(fly_time) and fly_time > 0:
            fly_times.append(fly_time)

    return max(fly_times) if fly_times else 0.0


def _minutes_between(start: datetime, end: datetime) -> int:
    """HH:MM に丸めた 2 時刻の分差を返す。日跨ぎは呼び出し側で考慮する。"""

    return max(0, int((end - start).total_seconds() // 60))


def _parse_dji_log(content: bytes) -> tuple[FlightSummary, dict[str, Any]]:
    """DJI ログのバイナリを解析して FlightSummary を返す。

    例外は呼び出し側で HTTP 422 に変換する。戻り値の dict はログ出力用の
    メタデータ（バージョン・フレーム数・解析モード）で、レスポンスには含めない。
    """

    log = DJILog.from_bytes(content)
    details = log.details
    version = int(getattr(log, "version", 0))
    frames = _load_frames(log)

    start_time = getattr(details, "start_time", None)
    if not isinstance(start_time, datetime):
        raise ValueError("missing start_time")
    if start_time.tzinfo is None:
        # pydjirecord は UTC aware を返すが、将来の実装差に備える。
        start_time = start_time.replace(tzinfo=timezone.utc)
    start_jst = start_time.astimezone(JST)
    if start_jst.year < MIN_LOG_YEAR:
        raise ValueError("invalid start_time")

    total_seconds = getattr(details, "total_time", 0.0)
    try:
        total_seconds = float(total_seconds)
    except (TypeError, ValueError):
        total_seconds = 0.0
    if not math.isfinite(total_seconds) or total_seconds <= 0:
        total_seconds = _seconds_from_frames(frames)
    if (
        not math.isfinite(total_seconds)
        or total_seconds <= 0
        or total_seconds > MAX_FLIGHT_SECONDS
    ):
        raise ValueError("invalid flight duration")

    # 表示は HH:MM のため秒を切り捨てる。離着陸が同一分になった短時間飛行は
    # 1 分として扱い、PWA 側の「飛行時間 0 分」を避ける。
    takeoff_dt = start_jst.replace(second=0, microsecond=0)
    landing_dt = (start_jst + timedelta(seconds=total_seconds)).astimezone(JST)
    landing_dt = landing_dt.replace(second=0, microsecond=0)
    duration_minutes = _minutes_between(takeoff_dt, landing_dt)
    if duration_minutes <= 0:
        duration_minutes = 1
        landing_dt = takeoff_dt + timedelta(minutes=1)

    coords = _first_valid_gps(frames)
    if coords is None:
        lat = getattr(details, "latitude", None)
        lng = getattr(details, "longitude", None)
        if _valid_coordinate(lat, lng):
            coords = (float(lat), float(lng))
    if coords is None:
        raise ValueError("no valid GPS")

    lat, lng = round(coords[0], 6), round(coords[1], 6)
    summary = FlightSummary(
        date=start_jst.date().isoformat(),
        takeoff_time=takeoff_dt.strftime("%H:%M"),
        landing_time=landing_dt.strftime("%H:%M"),
        duration_minutes=duration_minutes,
        lat=lat,
        lng=lng,
        takeoff_place=f"{lat:.6f}, {lng:.6f}",
    )
    return summary, {
        "version": version,
        "frame_count": len(frames),
        "mode": "frames" if frames else "details",
    }


def _unreadable_log_error() -> HTTPException:
    """DJI ログとして解釈できない場合の安全側エラー（ダミー値は返さない）。"""

    return HTTPException(
        status_code=422,
        detail={
            "code": "dji_log_unreadable",
            "message": (
                "DJIの飛行ログとして解析できませんでした。"
                "DJI Fly / DJI GO が出力した .txt ファイルを選択してください。"
            ),
        },
    )


def _to_response(filename: str, summary: FlightSummary) -> dict[str, object]:
    """PWA の applyDjiLogToForm() が期待するフラットな JSON を組み立てる。"""

    return {
        "fileName": filename,
        "date": summary.date,
        "takeoffTime": summary.takeoff_time,
        "landingTime": summary.landing_time,
        "durationMinutes": summary.duration_minutes,
        "lat": summary.lat,
        "lng": summary.lng,
        "takeoffPlace": summary.takeoff_place,
    }


def _overpass_query(lat: float, lon: float, radius: int) -> str:
    """指定座標の周辺にある鉄塔（node）と送電線（way）を取得するクエリ。"""

    return (
        "[out:json][timeout:10];\n"
        "(\n"
        f'  node["power"="tower"](around:{radius},{lat:.6f},{lon:.6f});\n'
        f'  way["power"="line"](around:{radius},{lat:.6f},{lon:.6f});\n'
        ");\n"
        "out body;\n"
        ">\n"
        "out skel qt;"
    )


def _overpass_error(code: str, message: str, status_code: int) -> HTTPException:
    """PWA が表示できる構造化エラーを返す。"""

    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _overpass_available() -> bool:
    """サーキットブレーカー: 直近の失敗から一定時間は Overpass を試さない。"""

    return time.monotonic() >= _OVERPASS_DOWN_UNTIL


def _mark_overpass_down(reason: str) -> None:
    """Overpass を一時的に使用停止にする（毎回6秒待たされるのを防ぐ）。"""

    global _OVERPASS_DOWN_UNTIL
    _OVERPASS_DOWN_UNTIL = time.monotonic() + OVERPASS_COOLDOWN_SECONDS
    logger.warning(
        "overpass disabled for %.0fs: reason=%s", OVERPASS_COOLDOWN_SECONDS, reason
    )


def _request_overpass(query: str) -> dict[str, Any]:
    """Overpass API（`OVERPASS_API_URLS` の順）へクエリを送り、JSON を返す。

    混雑・クォータ超過時は 406/429/502/503/504 が返るため、次のミラーへ切り替える。
    タイムアウトは待ち時間が倍になるだけで成功率が上がらないためリトライせず、
    サーキットブレーカーを開いて以後のリクエストを OSM API へ回す。
    クエリ以外の個人情報は送信しない。
    """

    if not _overpass_available():
        raise _overpass_error(
            "overpass_cooldown",
            "Overpass API を一時的に使用停止中です。時間をおいて再試行してください。",
            503,
        )

    last_status: int | None = None

    for url in OVERPASS_API_URLS:
        try:
            response = requests.post(
                url,
                data={"data": query},
                headers={
                    "User-Agent": f"{SERVICE_NAME}/0.4.0",
                    "Accept": "application/json",
                },
                timeout=OVERPASS_REQUEST_TIMEOUT,
            )
        except requests.Timeout as exc:
            logger.warning("overpass timeout: url=%s", url)
            _mark_overpass_down("timeout")
            raise _overpass_error(
                "overpass_timeout",
                "周辺インフラ情報の取得がタイムアウトしました。時間をおいて再試行してください。",
                504,
            ) from exc
        except requests.RequestException as exc:
            logger.warning(
                "overpass request failed: url=%s error=%s", url, type(exc).__name__
            )
            continue

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                _mark_overpass_down("invalid_json")
                raise _overpass_error(
                    "overpass_invalid_response",
                    "周辺インフラ情報の応答を解析できませんでした。時間をおいて再試行してください。",
                    502,
                ) from exc

            if not isinstance(payload, dict):
                _mark_overpass_down("invalid_payload")
                raise _overpass_error(
                    "overpass_invalid_response",
                    "周辺インフラ情報の応答形式が不正です。",
                    502,
                )
            return payload

        last_status = response.status_code
        logger.warning("overpass HTTP error: url=%s status=%d", url, last_status)

        if last_status not in OVERPASS_TRANSIENT_STATUS:
            _mark_overpass_down(f"http_{last_status}")
            raise _overpass_error(
                "overpass_unavailable",
                "周辺インフラ情報を取得できませんでした。時間をおいて再試行してください。",
                502,
            )

    _mark_overpass_down(f"http_{last_status}" if last_status else "connection_failed")
    if last_status in (429, 503):
        raise _overpass_error(
            "overpass_busy",
            "Overpass API が混雑しています。時間をおいて再試行してください。",
            503,
        )
    raise _overpass_error(
        "overpass_unavailable",
        "周辺インフラ情報を取得できませんでした。時間をおいて再試行してください。",
        502,
    )


def _count_overpass_elements(payload: dict[str, Any]) -> tuple[int, int]:
    """Overpass の elements から鉄塔と送電線を数える。"""

    elements = payload.get("elements", [])
    if not isinstance(elements, list):
        raise _overpass_error(
            "overpass_invalid_response",
            "周辺インフラ情報の応答形式が不正です。",
            502,
        )

    towers_count = 0
    lines_count = 0
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags")
        if not isinstance(tags, dict):
            continue
        power = tags.get("power")
        if power == "tower":
            towers_count += 1
        elif power == "line":
            lines_count += 1
    return towers_count, lines_count


def _osm_bbox(lat: float, lon: float, radius: int) -> tuple[float, float, float, float]:
    """半径 radius メートルを覆う bbox（min_lon, min_lat, max_lon, max_lat）。"""

    lat_delta = (radius / 111_320.0) * 1.05
    cos_lat = max(0.01, abs(math.cos(math.radians(lat))))
    lon_delta = (radius / (111_320.0 * cos_lat)) * 1.05
    return (
        max(-180.0, lon - lon_delta),
        max(-90.0, lat - lat_delta),
        min(180.0, lon + lon_delta),
        min(90.0, lat + lat_delta),
    )


def _local_xy_m(
    lat: float, lon: float, origin_lat: float, origin_lon: float
) -> tuple[float, float]:
    """緯度経度を、原点からの局所平面（メートル）へ近似変換する。"""

    x = math.radians(lon - origin_lon) * 6_371_000.0 * math.cos(math.radians(origin_lat))
    y = math.radians(lat - origin_lat) * 6_371_000.0
    return x, y


def _point_segment_distance_m(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> float:
    """点 P と線分 AB の最短距離（メートル）。"""

    dx = bx - ax
    dy = by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _count_osm_api_elements(
    root: ET.Element, lat: float, lon: float, radius: int
) -> tuple[int, int]:
    """OSM API の XML から、半径内の鉄塔と送電線を数える。"""

    nodes: dict[str, tuple[float, float]] = {}
    for node in root.findall("node"):
        node_id = node.get("id")
        try:
            node_lat = float(node.get("lat", ""))
            node_lon = float(node.get("lon", ""))
        except (TypeError, ValueError):
            continue
        if node_id:
            nodes[node_id] = (node_lat, node_lon)

    towers_count = 0
    for node in root.findall("node"):
        tags = {tag.get("k"): tag.get("v") for tag in node.findall("tag")}
        if tags.get("power") != "tower":
            continue
        node_id = node.get("id")
        if not node_id or node_id not in nodes:
            continue
        node_lat, node_lon = nodes[node_id]
        x, y = _local_xy_m(node_lat, node_lon, lat, lon)
        if math.hypot(x, y) <= radius:
            towers_count += 1

    lines_count = 0
    for way in root.findall("way"):
        tags = {tag.get("k"): tag.get("v") for tag in way.findall("tag")}
        if tags.get("power") != "line":
            continue

        coords = []
        for nd in way.findall("nd"):
            ref = nd.get("ref")
            if ref and ref in nodes:
                coords.append(nodes[ref])
        if not coords:
            continue

        points = [_local_xy_m(node_lat, node_lon, lat, lon) for node_lat, node_lon in coords]
        if len(points) == 1:
            hit = math.hypot(points[0][0], points[0][1]) <= radius
        else:
            hit = any(
                _point_segment_distance_m(0.0, 0.0, ax, ay, bx, by) <= radius
                for (ax, ay), (bx, by) in zip(points, points[1:])
            )
        if hit:
            lines_count += 1

    return towers_count, lines_count


def _fetch_osm_api_counts(lat: float, lon: float, radius: int) -> tuple[int, int]:
    """公式 OSM API の bbox から鉄塔・送電線を数える。

    小半径では Overpass より速く安定しているため、既定の取得元として使う。
    bbox は半径から算出し、要素は中心からの距離で絞り込む。
    """

    min_lon, min_lat, max_lon, max_lat = _osm_bbox(lat, lon, radius)
    url = f"{OSM_API_URL}?bbox={min_lon:.7f},{min_lat:.7f},{max_lon:.7f},{max_lat:.7f}"

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": f"{SERVICE_NAME}/0.4.0",
                "Accept": "application/xml",
            },
            timeout=OSM_API_TIMEOUT,
        )
    except requests.Timeout as exc:
        raise _overpass_error(
            "osm_api_timeout",
            "周辺インフラ情報の取得がタイムアウトしました。時間をおいて再試行してください。",
            504,
        ) from exc
    except requests.RequestException as exc:
        raise _overpass_error(
            "osm_api_unavailable",
            "周辺インフラ情報を取得できませんでした。通信状況を確認して再試行してください。",
            502,
        ) from exc

    if response.status_code in (400, 413):
        # OSM API は bbox 内の node が 5 万件を超えると 400 を返す。
        # 半径 1500m 以上を市街地で指定すると該当するため、理由を明示する。
        logger.warning(
            "OSM API rejected bbox: status=%d radius=%d", response.status_code, radius
        )
        raise _overpass_error(
            "osm_api_area_too_large",
            "検索範囲が広すぎて周辺インフラを取得できませんでした。"
            "半径を小さくするか、時間をおいて再試行してください。",
            503,
        )

    if response.status_code != 200:
        logger.warning("OSM API fallback failed: status=%d", response.status_code)
        raise _overpass_error(
            "osm_api_unavailable",
            "周辺インフラ情報を取得できませんでした。時間をおいて再試行してください。",
            502 if response.status_code < 500 else 503,
        )

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise _overpass_error(
            "osm_api_invalid_response",
            "周辺インフラ情報の応答を解析できませんでした。",
            502,
        ) from exc

    return _count_osm_api_elements(root, lat, lon, radius)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """起動確認用のヘルスチェック。"""
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/api/backup")
async def save_backup(request: Request) -> dict[str, Any]:
    """PWA の localStorage バックアップを ``data/backup.json`` に保存する。

    クライアントから受け取った JSON をそのまま（再シリアライズせず）書き込む。
    個人情報を含むため、ディレクトリは 0700、ファイルは 0600 にする。
    """
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="バックアップデータが空です。")
    if len(body) > MAX_BACKUP_BYTES:
        raise HTTPException(
            status_code=413,
            detail="バックアップが大きすぎます（上限10MB）。",
        )

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="JSONの形式が正しくありません。",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="バックアップはJSONオブジェクトで送信してください。",
        )

    try:
        async with BACKUP_LOCK:
            DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(DATA_DIR, 0o700)
            BACKUP_TMP_PATH.write_bytes(body)
            os.chmod(BACKUP_TMP_PATH, 0o600)
            BACKUP_TMP_PATH.replace(BACKUP_PATH)
    except OSError as exc:
        logger.exception("failed to save backup: %s", BACKUP_PATH)
        raise HTTPException(
            status_code=500,
            detail="バックアップを保存できませんでした。",
        ) from exc
    finally:
        if BACKUP_TMP_PATH.exists():
            try:
                BACKUP_TMP_PATH.unlink()
            except OSError:
                pass

    saved_at = datetime.now(JST).isoformat(timespec="seconds")
    logger.info("backup saved: %s (%d bytes)", BACKUP_PATH, len(body))
    return {"ok": True, "bytes": len(body), "savedAt": saved_at}


@app.get("/api/backup")
async def get_backup() -> JSONResponse:
    """``data/backup.json`` を返す。ファイルが無い場合は空オブジェクト。"""
    if not BACKUP_PATH.is_file():
        return JSONResponse(content={}, headers={"Cache-Control": "no-store"})

    try:
        raw = BACKUP_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.exception("failed to read backup: %s", BACKUP_PATH)
        raise HTTPException(
            status_code=500,
            detail="バックアップを読み込めませんでした。",
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=500,
            detail="バックアップの形式が不正です。",
        )
    return JSONResponse(content=data, headers={"Cache-Control": "no-store"})


@app.get(
    "/api/check-infrastructure",
    response_model=InfrastructureRisk,
    summary="周辺の送電線・鉄塔を確認する",
    responses={
        422: {"description": "緯度・経度・半径の検証エラー"},
        502: {"description": "Overpass API への接続失敗・不正な応答"},
        503: {"description": "Overpass API の混雑"},
        504: {"description": "Overpass API のタイムアウト"},
    },
)
def check_infrastructure(
    lat: float = Query(..., ge=-90.0, le=90.0, description="緯度"),
    lon: float = Query(..., ge=-180.0, le=180.0, description="経度"),
    radius: int = Query(300, ge=50, le=2000, description="検索半径（メートル）"),
) -> InfrastructureRisk:
    """指定座標から半径 radius メートル以内の鉄塔・送電線を数える。

    Overpass API の `power=tower`（node）と `power=line`（way）を対象にし、
    1 つ以上あれば `power_risk_detected=true` を返す。PWA はこの結果を
    赤（警告）／緑（安全）のアラートとして表示する。

    半径が `OSM_API_PREFERRED_MAX_RADIUS` 以下なら、応答が速く安定している
    公式 OSM API を先に試し、失敗時のみ Overpass へフォールバックする。
    これを逆にすると、混雑した Overpass で十数秒待たされてから OSM API に
    落ちるため、スマホ側で「固まった」ように見えてしまう。
    """

    started = time.monotonic()
    deadline = started + INFRA_DEADLINE_SECONDS

    providers = (
        ("overpass", "osm-api")
        if radius > OSM_API_PREFERRED_MAX_RADIUS
        else ("osm-api", "overpass")
    )
    towers_count = 0
    lines_count = 0
    source = "unknown"
    failures: list[HTTPException] = []

    for provider in providers:
        if time.monotonic() >= deadline:
            logger.warning(
                "check-infrastructure deadline exceeded before provider=%s", provider
            )
            break
        try:
            if provider == "overpass":
                payload = _request_overpass(_overpass_query(lat, lon, radius))
                towers_count, lines_count = _count_overpass_elements(payload)
            else:
                towers_count, lines_count = _fetch_osm_api_counts(lat, lon, radius)
            source = provider
            break
        except HTTPException as exc:
            if exc.status_code not in (502, 503, 504):
                raise
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else "unknown"
            logger.warning(
                "infra provider failed: provider=%s status=%d code=%s; trying fallback",
                provider,
                exc.status_code,
                code,
            )
            failures.append(exc)

    if source == "unknown":
        if failures:
            # 最後に失敗した取得元のエラーを PWA に返す（原因が最も具体的）。
            raise failures[-1]
        raise _overpass_error(
            "infra_deadline_exceeded",
            "周辺インフラ情報の取得に時間がかかりすぎました。時間をおいて再試行してください。",
            504,
        )

    power_risk_detected = towers_count > 0 or lines_count > 0
    if power_risk_detected:
        message = (
            f"半径{radius}m以内に鉄塔{towers_count}基・送電線{lines_count}件を検知しました。"
            "飛行前に離隔距離・電磁干渉・緊急時の退避経路を確認してください。"
        )
    else:
        message = "周辺インフラ（送電線・鉄塔）の異常なし"

    elapsed_ms = int((time.monotonic() - started) * 1000)

    logger.info(
        "check-infrastructure: lat=%.6f lon=%.6f radius=%d towers=%d lines=%d "
        "source=%s elapsed_ms=%d",
        lat,
        lon,
        radius,
        towers_count,
        lines_count,
        source,
        elapsed_ms,
    )

    return InfrastructureRisk(
        power_risk_detected=power_risk_detected,
        towers_count=towers_count,
        lines_count=lines_count,
        message=message,
        source=source,
        elapsed_ms=elapsed_ms,
    )


@app.post("/api/parse-log")
async def parse_log(
    file: UploadFile = File(..., description="DJI の飛行ログ（.txt）"),
) -> dict[str, object]:
    """DJI ログをバイナリ解析し、JST の飛行サマリーを返す。

    `.txt` 以外は 400。DJI ログとして解析できない `.txt` は、誤った飛行記録を
    防ぐためダミー値を返さず HTTP 422 と詳細コードを返す。
    """
    filename = safe_filename(file.filename)

    if not filename.lower().endswith(".txt"):
        raise HTTPException(
            status_code=400,
            detail=".txt ファイルを選択してください。",
        )

    try:
        content = await file.read()
    finally:
        await file.close()

    # 内容はログに出さない（個人情報・飛行経路の漏えい防止）。
    logger.info(
        "parse-log received: filename=%s bytes=%d",
        filename,
        len(content),
    )

    if not content:
        raise _unreadable_log_error()

    try:
        summary, meta = _parse_dji_log(content)
    except Exception as exc:  # noqa: BLE001 - 例外詳細はクライアントへ返さない
        logger.warning(
            "parse-log failed: filename=%s bytes=%d error=%s",
            filename,
            len(content),
            type(exc).__name__,
        )
        raise _unreadable_log_error() from exc

    logger.info(
        "parse-log parsed: filename=%s version=%s frames=%d mode=%s",
        filename,
        meta["version"],
        meta["frame_count"],
        meta["mode"],
    )
    return _to_response(filename, summary)


def _excel_filename(date_value: str) -> str:
    """Content-Disposition 用の安全な ASCII ファイル名を組み立てる。"""

    safe_date = re.sub(r"[^0-9A-Za-z._-]+", "_", date_value).strip("._")
    return f"flight_record_{safe_date or 'unknown'}.xlsx"


@app.post(
    "/api/export-log",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {EXCEL_MEDIA_TYPE: {}},
            "description": "飛行日誌の Excel ファイル",
        },
        422: {"description": "入力値の検証エラー"},
        500: {"description": "テンプレート読み込み・Excel 生成エラー"},
    },
)
def export_log(record: FlightRecord) -> StreamingResponse:
    """飛行日誌テンプレートへ入力を書き込み、Excel を返す。

    生成物は `io.BytesIO` 上で完結させ、ディスクへは保存しない。セル番地は
    国交省様式1の公式テンプレートに合わせてあり、変更する場合は README の
    対応表と `template/flight_record_template.xlsx` も更新すること。
    """

    if not TEMPLATE_PATH.is_file():
        logger.error("export-log template missing: %s", TEMPLATE_PATH)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "template_missing",
                "message": "飛行日誌テンプレートが見つかりません。",
            },
        )

    workbook = None
    buffer = BytesIO()
    try:
        workbook = load_workbook(TEMPLATE_PATH)
        sheet = workbook.active

        # 国交省様式1「無人航空機の飛行記録」の入力セル。
        sheet["B3"] = record.registration_id
        sheet["B5"] = record.date
        sheet["D5"] = record.operator_name
        sheet["G5"] = record.purpose
        sheet["H5"] = record.location
        sheet["I5"] = record.location
        sheet["L5"] = record.takeoff_time
        sheet["M5"] = record.landing_time
        sheet["O5"] = record.total_time
        sheet["Q5"] = f"{record.safety_measures} / {record.notes}"

        workbook.save(buffer)
    except Exception as exc:  # noqa: BLE001 - 例外詳細はクライアントへ返さない
        logger.exception(
            "export-log failed: date=%s template=%s",
            record.date,
            TEMPLATE_PATH,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code": "excel_export_failed",
                "message": "飛行日誌の生成に失敗しました。",
            },
        ) from exc
    finally:
        if workbook is not None:
            workbook.close()

    buffer.seek(0)
    filename = _excel_filename(record.date)
    logger.info(
        "export-log generated: date=%s filename=%s bytes=%d",
        record.date,
        filename,
        buffer.getbuffer().nbytes,
    )

    return StreamingResponse(
        buffer,
        media_type=EXCEL_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(buffer.getbuffer().nbytes),
        },
    )

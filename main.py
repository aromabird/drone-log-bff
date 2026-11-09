"""drone-log PWA 用 BFF.

`POST /api/parse-log` で DJI の `.txt` ログをバイナリ解析し、PWA が使う
日付・離着陸時刻（JST）・飛行時間・離陸地点を返す。`POST /api/export-log` では
様式1の Excel テンプレートへ入力を書き込み、オンメモリで `.xlsx` を返す。

解析には `pydjirecord` を使う。DJI の内部時刻は UTC で記録されるため、
レスポンスは必ず JST（UTC+9）へ変換する。v13 以降のフレーム（GPS 軌跡）は
DJI の API キーが必要だが、Details（日時・座標・飛行時間）はキーなしでも
復号できる。API キーは環境変数 `DJI_API_KEY` から読み、未設定時は Details を
使う。アップロードされた内容はメモリ上で読み捨て、ディスクへ保存しない。
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
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
    ),
    version="0.3.1",
)

# プロトタイプ用の CORS 設定。PWA は cookie を使わないため credentials は無効。
# 本番では allow_origins を GitHub Pages とローカルオリジンに限定すること。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # PWA がダウンロード時のファイル名を参照できるようにする。
    expose_headers=["Content-Disposition"],
)

Coordinates = tuple[float, float]


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


@app.get("/api/health")
async def health() -> dict[str, str]:
    """起動確認用のヘルスチェック。"""
    return {"status": "ok", "service": SERVICE_NAME}


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

"""drone-log PWA 用 BFF（プロトタイプ）.

`POST /api/parse-log` で DJI の `.txt` ログを受け取り、PWA の
`mockParseDjiLog()` と同じ形のダミー JSON を返す。

実際のログ解析は未実装。アップロードされた内容はメモリ上で読み捨て、
ディスクへ保存しない。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

SERVICE_NAME = "drone-log-bff"

# 日本標準時（UTC+9）。日本には夏時間がないため固定オフセットでよい。
JST = timezone(timedelta(hours=9), name="JST")

# PWA の mockParseDjiLog() と揃えたダミー値。
MOCK_TAKEOFF_TIME = "10:00"
MOCK_LANDING_TIME = "10:15"
MOCK_DURATION_MINUTES = 15
MOCK_LAT = 35.8988
MOCK_LNG = 140.6385
MOCK_TAKEOFF_PLACE = f"{MOCK_LAT}, {MOCK_LNG}"

logger = logging.getLogger(SERVICE_NAME)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(
    title="Drone Log BFF",
    description=(
        "ドローン飛行日誌 PWA のバックエンド・プロトタイプ。"
        "DJI ログの解析はモックで、固定のダミー値を返します。"
    ),
    version="0.1.0",
)

# プロトタイプ用の CORS 設定。PWA は cookie を使わないため credentials は無効。
# 本番では allow_origins を GitHub Pages とローカルオリジンに限定すること。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def jst_today() -> str:
    """JST の今日を YYYY-MM-DD で返す。"""
    return datetime.now(JST).date().isoformat()


def safe_filename(filename: str | None) -> str:
    """アップロードされたファイル名からディレクトリ部分を除く。"""
    if not filename:
        return ""
    # POSIX / Windows のどちらの区切り文字にも対応する。
    return filename.replace("\\", "/").rsplit("/", 1)[-1].strip()


@app.get("/api/health")
async def health() -> dict[str, str]:
    """起動確認用のヘルスチェック。"""
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/api/parse-log")
async def parse_log(
    file: UploadFile = File(..., description="DJI の飛行ログ（.txt）"),
) -> dict[str, object]:
    """DJI ログを受け取り、UI 確認用のダミー解析結果を返す。

    現時点ではファイル内容を解析しない。拡張子 `.txt` の検証と、
    アップロードが実際に届いたことの確認だけを行う。
    """
    filename = safe_filename(file.filename)

    if not filename.lower().endswith(".txt"):
        raise HTTPException(
            status_code=400,
            detail=".txt ファイルを選択してください。",
        )

    content = await file.read()
    await file.close()

    # 内容はログに出さない（個人情報・飛行経路の漏えい防止）。
    logger.info(
        "parse-log received: filename=%s bytes=%d",
        filename,
        len(content),
    )

    return {
        "fileName": filename,
        "date": jst_today(),
        "takeoffTime": MOCK_TAKEOFF_TIME,
        "landingTime": MOCK_LANDING_TIME,
        "durationMinutes": MOCK_DURATION_MINUTES,
        "lat": MOCK_LAT,
        "lng": MOCK_LNG,
        "takeoffPlace": MOCK_TAKEOFF_PLACE,
    }

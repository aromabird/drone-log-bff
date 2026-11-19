#!/usr/bin/env bash
# /api/flight-records（status 付き飛行記録）の保存・検証テスト。
# 本番の data/ を壊さないよう、一時ディレクトリを DRONE_LOG_DATA_DIR に指定して
# 別ポートで BFF を起動する。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
UVICORN="$ROOT_DIR/.venv/bin/uvicorn"
TMP_DIR="$(mktemp -d)"
PORT="${FLIGHT_RECORDS_TEST_PORT:-8012}"
BASE_URL="http://127.0.0.1:$PORT"
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

if [ ! -x "$PYTHON" ] || [ ! -x "$UVICORN" ]; then
  echo "ERROR: .venv が見つかりません。README のセットアップを実行してください。" >&2
  exit 1
fi

export DRONE_LOG_DATA_DIR="$TMP_DIR/data"
cd "$ROOT_DIR"
"$UVICORN" main:app --host 127.0.0.1 --port "$PORT" >"$TMP_DIR/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 40); do
  if curl -fsS --max-time 2 "$BASE_URL/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
if ! curl -fsS --max-time 2 "$BASE_URL/api/health" >/dev/null 2>&1; then
  echo "ERROR: テスト用 BFF を起動できませんでした（port $PORT）。" >&2
  cat "$TMP_DIR/server.log" >&2
  exit 1
fi

# 13項目の点検データを組み立てる
"$PYTHON" - "$TMP_DIR/preflight.json" "$TMP_DIR/completed.json" "$TMP_DIR/missing_post.json" "$TMP_DIR/missing_items.json" "$TMP_DIR/export.json" <<'PY'
import json
import sys

items = [
    "機体全般", "プロペラ", "フレーム",
    "通信系統", "推進系統", "電源系統", "自動制御系統", "操縦装置",
    "バッテリー・燃料", "機体識別表示", "リモートID", "灯火", "カメラ",
]


def inspection(kind, item_names=None):
    names = item_names if item_names is not None else items
    return {
        "type": kind,
        "inspection_date": "2026-11-15",
        "inspector": "テスト 太郎",
        "result": "合格",
        "memo": "",
        "items": {name: "OK" for name in names},
    }


base = {
    "status": "pre_flight_ready",
    "date": "2026-11-15",
    "pilot_id": "PLT-0001",
    "drone_id": "DRN-0001",
    "takeoff_place": "千葉県香取市小見川",
    "landing_place": "",
    "lat": 35.681236,
    "lng": 139.767125,
    "takeoff_time": "",
    "landing_time": "",
    "duration_min": 0,
    "specific_flags": ["非特定飛行"],
    "has_clear_zone": True,
    "category": "カテゴリーⅠ",
    "purpose": "空撮",
    "safety_event": "なし",
    "inspections": [inspection("飛行前")],
    "preflight_saved_at": "2026-11-15 10:15:00",
    "completed_at": "",
}

completed = dict(base)
completed.update({
    "status": "completed",
    "takeoff_time": "10:00",
    "landing_time": "10:45",
    "duration_min": 45,
    "completed_at": "2026-11-15 10:50:00",
    "inspections": [inspection("飛行前"), inspection("飛行後")],
})

missing_post = dict(completed)
missing_post["inspections"] = [inspection("飛行前")]

missing_items = dict(base)
missing_items["inspections"] = [inspection("飛行前", items[:-1])]

export = {
    "date": "2026-11-15",
    "operator_name": "テスト 太郎",
    "registration_id": "JU1234567890",
    "purpose": "空撮",
    "location": "テスト飛行場",
    "takeoff_time": "10:00",
    "landing_time": "10:45",
    "total_time": "45分",
    "safety_measures": "立入管理措置: 講じる",
    "notes": "status 付き",
    "status": "completed",
    "inspections": [inspection("飛行前"), inspection("飛行後")],
}

for path, payload in (
    (sys.argv[1], base),
    (sys.argv[2], completed),
    (sys.argv[3], missing_post),
    (sys.argv[4], missing_items),
    (sys.argv[5], export),
):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
PY

echo "1) GET 空 → records: []"
curl -fsS --max-time 5 "$BASE_URL/api/flight-records" -o "$TMP_DIR/list_empty.json"
"$PYTHON" - "$TMP_DIR/list_empty.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["ok"] is True, data
assert data["total"] == 0, data
assert data["records"] == [], data
print("   empty OK")
PY

echo "2) POST pre_flight_ready（離着陸時刻なし）→ 200 / FLT-0001"
curl -fsS --max-time 5 -X POST -H 'Content-Type: application/json' \
  --data-binary "@$TMP_DIR/preflight.json" \
  "$BASE_URL/api/flight-records" -o "$TMP_DIR/preflight_res.json"
"$PYTHON" - "$TMP_DIR/preflight_res.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["ok"] is True, data
record = data["record"]
assert record["status"] == "pre_flight_ready", record
assert record["flight_id"] == "FLT-0001", record
assert record["takeoff_time"] == "", record
assert len(record["inspections"]) == 1, record
assert record["created_at"] and record["updated_at"], record
print("   pre_flight_ready OK:", record["flight_id"])
PY

echo "3) POST completed だが飛行後点検なし → 422"
STATUS=$(curl -s -o "$TMP_DIR/err1.json" -w '%{http_code}' --max-time 5 \
  -X POST -H 'Content-Type: application/json' \
  --data-binary "@$TMP_DIR/missing_post.json" \
  "$BASE_URL/api/flight-records")
[ "$STATUS" = "422" ] || { echo "ERROR: expected 422, got $STATUS"; cat "$TMP_DIR/err1.json"; exit 1; }
echo "   422 OK"

echo "4) POST 不正な status → 422"
"$PYTHON" - "$TMP_DIR/preflight.json" "$TMP_DIR/bad_status.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
data["status"] = "flying"
json.dump(data, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
PY
STATUS=$(curl -s -o "$TMP_DIR/err2.json" -w '%{http_code}' --max-time 5 \
  -X POST -H 'Content-Type: application/json' \
  --data-binary "@$TMP_DIR/bad_status.json" \
  "$BASE_URL/api/flight-records")
[ "$STATUS" = "422" ] || { echo "ERROR: expected 422, got $STATUS"; cat "$TMP_DIR/err2.json"; exit 1; }
echo "   422 OK"

echo "5) POST 点検項目が12件 → 422"
STATUS=$(curl -s -o "$TMP_DIR/err3.json" -w '%{http_code}' --max-time 5 \
  -X POST -H 'Content-Type: application/json' \
  --data-binary "@$TMP_DIR/missing_items.json" \
  "$BASE_URL/api/flight-records")
[ "$STATUS" = "422" ] || { echo "ERROR: expected 422, got $STATUS"; cat "$TMP_DIR/err3.json"; exit 1; }
echo "   422 OK"

echo "6) POST completed（flight_id 付き）→ upsert で 1 件のまま"
"$PYTHON" - "$TMP_DIR/completed.json" "$TMP_DIR/completed_with_id.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
data["flight_id"] = "FLT-0001"
json.dump(data, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
PY
curl -fsS --max-time 5 -X POST -H 'Content-Type: application/json' \
  --data-binary "@$TMP_DIR/completed_with_id.json" \
  "$BASE_URL/api/flight-records" -o "$TMP_DIR/completed_res.json"
"$PYTHON" - "$TMP_DIR/completed_res.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
record = data["record"]
assert record["flight_id"] == "FLT-0001", record
assert record["status"] == "completed", record
assert record["duration_min"] == 45, record
assert len(record["inspections"]) == 2, record
print("   upsert OK:", record["flight_id"], record["status"])
PY

curl -fsS --max-time 5 "$BASE_URL/api/flight-records" -o "$TMP_DIR/list_after.json"
"$PYTHON" - "$TMP_DIR/list_after.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["total"] == 1, data
assert data["records"][0]["status"] == "completed", data
print("   total=1 OK")
PY

echo "7) GET ?status=pre_flight_ready → 0 件 / ?status=completed → 1 件"
curl -fsS --max-time 5 "$BASE_URL/api/flight-records?status=pre_flight_ready" -o "$TMP_DIR/list_pending.json"
curl -fsS --max-time 5 "$BASE_URL/api/flight-records?status=completed" -o "$TMP_DIR/list_done.json"
"$PYTHON" - "$TMP_DIR/list_pending.json" "$TMP_DIR/list_done.json" <<'PY'
import json
import sys

pending = json.load(open(sys.argv[1], encoding="utf-8"))
done = json.load(open(sys.argv[2], encoding="utf-8"))
assert pending["total"] == 0, pending
assert done["total"] == 1, done
print("   filter OK")
PY

echo "8) 保存ファイルの権限が 600"
PERM=$(stat -f '%Lp' "$TMP_DIR/data/flight_records.json")
[ "$PERM" = "600" ] || { echo "ERROR: expected 600, got $PERM"; exit 1; }
echo "   permission OK (600)"

echo "9) export-log が status / inspections を受け入れる"
STATUS=$(curl -s -o "$TMP_DIR/export.xlsx" -w '%{http_code}' --max-time 10 \
  -X POST -H 'Content-Type: application/json' \
  --data-binary "@$TMP_DIR/export.json" \
  "$BASE_URL/api/export-log")
[ "$STATUS" = "200" ] || { echo "ERROR: export-log expected 200, got $STATUS"; exit 1; }
"$PYTHON" - "$TMP_DIR/export.xlsx" <<'PY'
import sys
import zipfile

assert zipfile.is_zipfile(sys.argv[1]), "xlsx が生成されていません"
print("   export-log OK")
PY

echo "すべての flight-records テストが成功しました。"

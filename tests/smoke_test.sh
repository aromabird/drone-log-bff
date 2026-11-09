#!/usr/bin/env bash
# drone-log-bff のスモークテスト（サーバー起動後に実行）
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SAMPLE="$ROOT_DIR/tests/sample_dji.txt"
MINIMAL="$ROOT_DIR/tests/fixtures/minimal_v14.txt"
INVALID="$ROOT_DIR/tests/invalid_dji.txt"
EXPORT_JSON="$ROOT_DIR/tests/export_sample.json"
PYTHON="$ROOT_DIR/.venv/bin/python"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

if [ ! -x "$PYTHON" ]; then
  echo "ERROR: $PYTHON が見つかりません。先に .venv を作成してください。" >&2
  exit 1
fi
for path in "$SAMPLE" "$MINIMAL" "$INVALID" "$EXPORT_JSON"; do
  if [ ! -f "$path" ]; then
    echo "ERROR: $path が見つかりません。" >&2
    exit 1
  fi
done

echo "1) health"
curl -fsS --max-time 5 "$BASE_URL/api/health" -o "$TMP_DIR/health.json"
"$PYTHON" - "$TMP_DIR/health.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["status"] == "ok", data
assert data["service"] == "drone-log-bff", data
print("   health OK:", data)
PY

echo "2) parse-log real DJI log (v4 / Details fallback)"
curl -fsS --max-time 15 -D "$TMP_DIR/headers.txt" \
  -X POST \
  -F "file=@$SAMPLE;type=text/plain" \
  "$BASE_URL/api/parse-log" \
  -o "$TMP_DIR/response.json"
"$PYTHON" - "$TMP_DIR/response.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
expected = {
    "fileName": "sample_dji.txt",
    "date": "2015-12-30",
    "takeoffTime": "04:05",
    "landingTime": "04:07",
    "durationMinutes": 2,
    "lat": 53.735879,
    "lng": -1.766518,
    "takeoffPlace": "53.735879, -1.766518",
}
assert data == expected, f"unexpected response: {data!r}"
print("   parse-log v4 OK:", data)
PY

if ! grep -qi '^content-type: application/json' "$TMP_DIR/headers.txt"; then
  echo "ERROR: Content-Type が application/json ではありません。" >&2
  cat "$TMP_DIR/headers.txt" >&2
  exit 1
fi

echo "3) parse-log encrypted DJI log (v14 / Details fallback)"
curl -fsS --max-time 15 \
  -X POST \
  -F "file=@$MINIMAL;type=text/plain" \
  "$BASE_URL/api/parse-log" \
  -o "$TMP_DIR/minimal.json"
"$PYTHON" - "$TMP_DIR/minimal.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
expected = {
    "fileName": "minimal_v14.txt",
    "date": "2021-05-26",
    "takeoffTime": "03:31",
    "landingTime": "03:36",
    "durationMinutes": 5,
    "lat": 41.3,
    "lng": 19.8,
    "takeoffPlace": "41.300000, 19.800000",
}
assert data == expected, f"unexpected response: {data!r}"
print("   parse-log v14 OK:", data)
PY

echo "4) reject non-txt"
STATUS="$(curl -sS --max-time 10 -o "$TMP_DIR/reject.json" -w '%{http_code}' \
  -X POST \
  -F "file=@$SAMPLE;filename=sample.csv;type=text/csv" \
  "$BASE_URL/api/parse-log")"
if [ "$STATUS" != "400" ]; then
  echo "ERROR: 400 を期待しましたが $STATUS でした。" >&2
  cat "$TMP_DIR/reject.json" >&2
  exit 1
fi
"$PYTHON" - "$TMP_DIR/reject.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert ".txt" in data.get("detail", ""), data
print("   reject OK:", data)
PY

echo "5) reject invalid .txt (safe fallback, no dummy values)"
STATUS="$(curl -sS --max-time 10 -o "$TMP_DIR/invalid.json" -w '%{http_code}' \
  -X POST \
  -F "file=@$INVALID;type=text/plain" \
  "$BASE_URL/api/parse-log")"
if [ "$STATUS" != "422" ]; then
  echo "ERROR: 422 を期待しましたが $STATUS でした。" >&2
  cat "$TMP_DIR/invalid.json" >&2
  exit 1
fi
"$PYTHON" - "$TMP_DIR/invalid.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
detail = data.get("detail")
assert isinstance(detail, dict), data
assert detail.get("code") == "dji_log_unreadable", data
assert "DJI" in detail.get("message", ""), data
print("   invalid .txt OK:", detail["code"])
PY

echo "6) missing file"
STATUS="$(curl -sS --max-time 10 -o "$TMP_DIR/missing.json" -w '%{http_code}' \
  -X POST \
  "$BASE_URL/api/parse-log")"
if [ "$STATUS" != "422" ]; then
  echo "ERROR: 422 を期待しましたが $STATUS でした。" >&2
  cat "$TMP_DIR/missing.json" >&2
  exit 1
fi
echo "   missing file OK: 422"

echo "7) CORS preflight"
curl -sS --max-time 10 -D "$TMP_DIR/cors.txt" -o /dev/null -X OPTIONS \
  -H "Origin: http://localhost:8811" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type" \
  "$BASE_URL/api/parse-log"
if ! grep -qi '^access-control-allow-origin: \*' "$TMP_DIR/cors.txt"; then
  echo "ERROR: access-control-allow-origin がありません。" >&2
  cat "$TMP_DIR/cors.txt" >&2
  exit 1
fi
if ! grep -qi '^access-control-allow-methods:.*POST' "$TMP_DIR/cors.txt"; then
  echo "ERROR: CORS で POST が許可されていません。" >&2
  cat "$TMP_DIR/cors.txt" >&2
  exit 1
fi
echo "   CORS OK"

echo "8) export-log Excel"
curl -fsS --max-time 15 -D "$TMP_DIR/export-headers.txt" \
  -X POST \
  -H "Content-Type: application/json" \
  --data-binary @"$EXPORT_JSON" \
  "$BASE_URL/api/export-log" \
  -o "$TMP_DIR/flight_record.xlsx"

if ! grep -qi '^content-type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' "$TMP_DIR/export-headers.txt"; then
  echo "ERROR: Excel の Content-Type が不正です。" >&2
  cat "$TMP_DIR/export-headers.txt" >&2
  exit 1
fi
if ! grep -qi '^content-disposition: attachment; filename="flight_record_2026-11-08.xlsx"' "$TMP_DIR/export-headers.txt"; then
  echo "ERROR: Content-Disposition が不正です。" >&2
  cat "$TMP_DIR/export-headers.txt" >&2
  exit 1
fi
"$PYTHON" - "$TMP_DIR/flight_record.xlsx" <<'PY'
import sys

from openpyxl import load_workbook

path = sys.argv[1]
workbook = load_workbook(path)
sheet = workbook.active
assert sheet.title == "飛行記録", f"unexpected sheet: {sheet.title!r}"
expected = {
    "B3": "JU1234567890",
    "B5": "2026-11-08",
    "D5": "普施 太郎",
    "G5": "空撮",
    "H5": "テスト飛行場",
    "I5": "テスト飛行場",
    "L5": "10:00",
    "M5": "10:15",
    "O5": "15分",
    "Q5": "立入管理措置: 講じる / 飛行前点検: 全項目OK / スモークテスト",
}
for cell, value in expected.items():
    actual = sheet[cell].value
    assert actual == value, f"{cell}: expected {value!r}, got {actual!r}"
workbook.close()
print("   export-log OK:", path)
PY

echo "ALL TESTS PASSED"

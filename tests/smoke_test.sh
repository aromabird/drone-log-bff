#!/usr/bin/env bash
# drone-log-bff のスモークテスト（サーバー起動後に実行）
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SAMPLE="$ROOT_DIR/tests/sample_dji.txt"
PYTHON="$ROOT_DIR/.venv/bin/python"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

if [ ! -x "$PYTHON" ]; then
  echo "ERROR: $PYTHON が見つかりません。先に .venv を作成してください。" >&2
  exit 1
fi
if [ ! -f "$SAMPLE" ]; then
  echo "ERROR: $SAMPLE が見つかりません。" >&2
  exit 1
fi

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

echo "2) parse-log success"
curl -fsS --max-time 10 -D "$TMP_DIR/headers.txt" \
  -X POST \
  -F "file=@$SAMPLE;type=text/plain" \
  "$BASE_URL/api/parse-log" \
  -o "$TMP_DIR/response.json"
"$PYTHON" - "$TMP_DIR/response.json" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone

data = json.load(open(sys.argv[1], encoding="utf-8"))
today_jst = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
expected = {
    "fileName": "sample_dji.txt",
    "date": today_jst,
    "takeoffTime": "10:00",
    "landingTime": "10:15",
    "durationMinutes": 15,
    "lat": 35.8988,
    "lng": 140.6385,
    "takeoffPlace": "35.8988, 140.6385",
}
assert data == expected, f"unexpected response: {data!r}"
print("   parse-log OK:", data)
PY

if ! grep -qi '^content-type: application/json' "$TMP_DIR/headers.txt"; then
  echo "ERROR: Content-Type が application/json ではありません。" >&2
  cat "$TMP_DIR/headers.txt" >&2
  exit 1
fi

echo "3) reject non-txt"
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

echo "4) missing file"
STATUS="$(curl -sS --max-time 10 -o "$TMP_DIR/missing.json" -w '%{http_code}' \
  -X POST \
  "$BASE_URL/api/parse-log")"
if [ "$STATUS" != "422" ]; then
  echo "ERROR: 422 を期待しましたが $STATUS でした。" >&2
  cat "$TMP_DIR/missing.json" >&2
  exit 1
fi
echo "   missing file OK: 422"

echo "5) CORS preflight"
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

echo "ALL TESTS PASSED"

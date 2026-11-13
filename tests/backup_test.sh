#!/usr/bin/env bash
# /api/backup の保存・取得テスト。本番の data/backup.json を壊さないよう、
# 一時ディレクトリを DRONE_LOG_DATA_DIR に指定して別ポートで BFF を起動する。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
UVICORN="$ROOT_DIR/.venv/bin/uvicorn"
TMP_DIR="$(mktemp -d)"
PORT="${BACKUP_TEST_PORT:-8011}"
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

echo "1) GET ファイル無し → {}"
curl -fsS --max-time 5 "$BASE_URL/api/backup" -o "$TMP_DIR/empty.json"
"$PYTHON" - "$TMP_DIR/empty.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    assert json.load(fh) == {}, "missing file should return {}"
print("   empty OK")
PY

echo "2) POST → 200 / 保存ファイルは送信内容と同一"
cat > "$TMP_DIR/payload.json" <<'JSON'
{"drone-log:mock:v1":"{\"pilots\":[{\"name\":\"テスト\"}]}","drone-log:last-pilot":"PLT-0001"}
JSON
curl -fsS --max-time 5 -X POST \
  -H "Content-Type: application/json" \
  --data-binary @"$TMP_DIR/payload.json" \
  "$BASE_URL/api/backup" \
  -o "$TMP_DIR/post.json"
"$PYTHON" - "$TMP_DIR/post.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
assert data.get("ok") is True, data
assert data.get("bytes", 0) > 0, data
assert data.get("savedAt"), data
print("   post OK:", data["savedAt"])
PY
cmp -s "$TMP_DIR/payload.json" "$TMP_DIR/data/backup.json"
echo "   raw body identical OK"

echo "3) GET → POST した JSON と同一"
curl -fsS --max-time 5 -D "$TMP_DIR/get.headers" "$BASE_URL/api/backup" -o "$TMP_DIR/get.json"
"$PYTHON" - "$TMP_DIR/payload.json" "$TMP_DIR/get.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    expected = json.load(fh)
with open(sys.argv[2], encoding="utf-8") as fh:
    actual = json.load(fh)
assert actual == expected, (actual, expected)
print("   get OK")
PY
if ! grep -qi '^cache-control: no-store' "$TMP_DIR/get.headers"; then
  echo "ERROR: GET /api/backup に Cache-Control: no-store がありません。" >&2
  cat "$TMP_DIR/get.headers" >&2
  exit 1
fi

echo "4) パーミッション（dir 700 / file 600）"
case "$(uname -s)" in
  Darwin)
    FILE_MODE="$(stat -f '%Lp' "$TMP_DIR/data/backup.json")"
    DIR_MODE="$(stat -f '%Lp' "$TMP_DIR/data")"
    ;;
  *)
    FILE_MODE="$(stat -c '%a' "$TMP_DIR/data/backup.json")"
    DIR_MODE="$(stat -c '%a' "$TMP_DIR/data")"
    ;;
esac
if [ "$FILE_MODE" != "600" ]; then
  echo "ERROR: backup.json のパーミッションが $FILE_MODE です。" >&2
  exit 1
fi
if [ "$DIR_MODE" != "700" ]; then
  echo "ERROR: data/ のパーミッションが $DIR_MODE です。" >&2
  exit 1
fi
echo "   permissions OK"

echo "5) 不正 JSON / 配列は 400、既存バックアップは保持"
STATUS="$(curl -sS --max-time 5 -o "$TMP_DIR/invalid.json" -w '%{http_code}' \
  -X POST -H "Content-Type: application/json" \
  --data-binary '{invalid' \
  "$BASE_URL/api/backup")"
if [ "$STATUS" != "400" ]; then
  echo "ERROR: 不正 JSON は 400 を期待しましたが $STATUS でした。" >&2
  cat "$TMP_DIR/invalid.json" >&2
  exit 1
fi
STATUS="$(curl -sS --max-time 5 -o "$TMP_DIR/array.json" -w '%{http_code}' \
  -X POST -H "Content-Type: application/json" \
  --data-binary '[]' \
  "$BASE_URL/api/backup")"
if [ "$STATUS" != "400" ]; then
  echo "ERROR: 配列は 400 を期待しましたが $STATUS でした。" >&2
  cat "$TMP_DIR/array.json" >&2
  exit 1
fi
curl -fsS --max-time 5 "$BASE_URL/api/backup" -o "$TMP_DIR/get2.json"
"$PYTHON" - "$TMP_DIR/payload.json" "$TMP_DIR/get2.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    expected = json.load(fh)
with open(sys.argv[2], encoding="utf-8") as fh:
    actual = json.load(fh)
assert actual == expected, "invalid POST must not overwrite the backup"
print("   invalid payloads OK")
PY

echo "ALL BACKUP TESTS PASSED"

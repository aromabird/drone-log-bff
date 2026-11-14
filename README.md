# drone-log-bff

ドローン飛行日誌 PWA（[`aromabird/drone-log`](https://github.com/aromabird/drone-log)）の
バックエンドです。FastAPI で `.txt` の DJI ログを受け取り、
[`pydjirecord`](https://github.com/rembish/pydjirecord) でバイナリ解析して、
PWA がそのまま使える飛行サマリー JSON を返します。

さらに `POST /api/export-log` で、国交省「無人航空機の飛行日誌の取扱要領」様式1に
準拠した Excel テンプレートへ入力を書き込み、`openpyxl` + `io.BytesIO` で
オンメモリ生成した `.xlsx` を返します（ディスクへは保存しません）。

`GET /api/check-infrastructure` では、指定座標の周辺にある送電線・鉄塔を
Overpass API（OpenStreetMap）から取得し、PWA のリスク判定用 JSON を返します。

`GET /api/reverse-geocode` では、指定座標の日本の住所を Google Maps Geocoding API
から取得し、「日本、〒…」を除いた住所文字列を返します。キーは BFF の `.env` だけで
管理します。

`POST /api/diagnose` では、飛行計画・気象・標高・DID・周辺インフラ・機体情報と
任意の現場写真から、Gemini API で飛行前の安全診断文を生成します。API キーは
BFF の環境変数（`.env`）だけで管理し、ブラウザへは渡しません。

DJI の内部時刻は UTC で記録されるため、日付・離着陸時刻は必ず JST（UTC+9）へ
変換して返します。アップロードされた内容はメモリ上で読み捨て、ディスクへ
保存しません。

## 必要環境

- Python 3.11 以上
- macOS の Homebrew Python を使う場合: `/opt/homebrew/bin/python3.11`

## セットアップ

```bash
cd drone-log-bff
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## 起動

ローカルだけで試す場合（この Mac のみ）:

```bash
cd drone-log-bff
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

Tailscale や LAN の別端末から使う場合（本番と同じ）:

```bash
cd drone-log-bff
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

- API ドキュメント: <http://127.0.0.1:8000/docs>
- ヘルスチェック: <http://127.0.0.1:8000/api/health>
- 直 IP での確認（tailnet 内）: <http://100.113.102.38:8000/api/health>

`uvicorn` の既定 bind は `127.0.0.1` です。`--host 0.0.0.0` を付けると
macOS の全インターフェース（LAN / Tailscale）で待ち受けます。認証は無いため、
macOS のファイアウォールと Tailscale の ACL で到達範囲を制御してください。

### 本番運用（launchd + Tailscale）

mac-mini-m4 では launchd の `com.aromabird.drone-log-bff` が起動を管理しています。

```text
~/Library/LaunchAgents/com.aromabird.drone-log-bff.plist
  ProgramArguments: .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
  RunAtLoad: true / KeepAlive: true
  StandardOutPath: /tmp/drone-log-bff.log
  StandardErrorPath: /tmp/drone-log-bff.err
```

PWA は HTTPS で配信されるため、BFF も HTTPS でアクセスできる必要があります。
`tailscale serve` が tailnet 内の HTTPS（443）を `127.0.0.1:8000` へプロキシします。

```bash
# 初回のみ（設定済みなら不要）
tailscale serve --bg http://127.0.0.1:8000
tailscale serve status

# 例: https://mac-mini-m4.tail95f7a2.ts.net/api/health
```

- `tailscale serve` は **tailnet 内のみ**に公開します（一般インターネットには出ません）。
- 認証が無いため `tailscale funnel` は使用しないでください。
- HTTPS の PWA から `http://100.x.x.x:8000` を呼ぶと、ブラウザの混在コンテンツ
  制限でブロックされます。PWA の設定タブには `https://…ts.net` を指定してください。

### v13 以降のフレーム（GPS 軌跡）を使う場合

v13 以降のログはフレームが暗号化されており、DJI の API キーが必要です。
キーを設定しなくても Details（日時・座標・飛行時間）は復号できるため、
本 BFF のレスポンスには影響しません。フレームから最初の有効な GPS を
使いたい場合だけ、起動前に環境変数を設定してください。

```bash
export DJI_API_KEY="<DJI Developer API Key>"
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

キーは DJI のキーチェーン取得 API に送信されます。ログやレスポンスには
出力しません。未設定時は Details の座標を使います。

### AI 飛行前診断（Gemini）の設定

`POST /api/diagnose` は Google の Gemini API（Generative Language API）を REST で
呼びます。SDK は追加しません。

```bash
# drone-log-bff/.env（Git 管理外・パーミッション 600）
GEMINI_API_KEY=<Google AI Studio で発行した API キー>
GEMINI_MODEL=gemini-3.5-flash-lite   # 任意（既定値も同じ）
```

- `.env` は BFF 直下に置くと起動時に `python-dotenv` が読みます。launchd などで
  環境変数を明示した場合は、そちらが優先されます。
- キーはレスポンス・ログ・PWA へ出しません。ログは `source / model / 文字数 /
  画像有無 / 処理時間` のみです。
- 既定モデル `gemini-3.5-flash-lite` が提供終了した場合は、`GEMINI_MODEL` を
  利用可能なモデル名へ変更してください（404 は `ai_model_not_found` を返します）。
- 写真は `source="photo"`、周辺マップは `source="map"` のときだけ送信します。
  PWA 側で写真は長辺 1200px、地図は表示中の領域（`html2canvas`・2倍解像度）を
  JPEG に変換し、BFF は base64 をそのまま Gemini へ渡します（ディスクへは保存しません）。

### 逆ジオコーディング（Google Maps Geocoding API）の設定

`GET /api/reverse-geocode` は Google Maps Geocoding API（`language=ja`）を REST で呼びます。

```bash
# drone-log-bff/.env（Git 管理外・パーミッション 600）
GOOGLE_MAPS_API_KEY=<Google Cloud で発行した API キー>
```

- Google Cloud Console で **Geocoding API** を有効化したプロジェクトのキーを使ってください。
- キーに API 制限をかける場合は **Geocoding API** を許可します。サーバーからの呼び出しなので
  HTTP リファラー制限は使えません（IP 制限を使う場合はこの Mac の固定 IP が必要です）。
- `.env` を変更したら BFF を再起動してください（launchd なら `launchctl kickstart`）。
- 未設定でも BFF は起動し、`/api/reverse-geocode` だけが
  `503 {"detail": {"code": "geocode_not_configured", ...}}` を返します。
- キーはレスポンス・ログ・PWA へ出しません。ログは `座標 / 住所の文字数 / 処理時間` のみです。
- 上流の `status` は HTTP ステータスへ変換します（`ZERO_RESULTS`→404、
  `OVER_QUERY_LIMIT`→503、`REQUEST_DENIED`→503、`INVALID_REQUEST`→502、タイムアウト→504）。
- PWA 側は BFF が失敗したとき、国土地理院の逆ジオコーダー（市区町村名）へフォールバックします。

## API

### `GET /api/health`

```json
{"status": "ok", "service": "drone-log-bff"}
```

### `GET /api/reverse-geocode`

緯度・経度から日本の住所を返します（PWA の GPS ボタン・座標検索・貼り付け変換が使用）。

- クエリパラメータ
  - `lat`（必須）: 緯度 `-90.0`〜`90.0`
  - `lng`（必須）: 経度 `-180.0`〜`180.0`
- 成功時: `200`

```json
{
  "ok": true,
  "address": "東京都千代田区丸の内１丁目",
  "formatted_address": "日本、〒100-0005 東京都千代田区丸の内１丁目",
  "lat": 35.681236,
  "lng": 139.767125,
  "place_id": "ChIJ...",
  "source": "google",
  "fetched_at": "2026-11-14T18:40:00+09:00"
}
```

- `address` は `formatted_address` から「日本」「〒xxx-xxxx」を除いた文字列です。
- エラーは `{"detail": {"code": "...", "message": "..."}}` 形式です。
  - `503 geocode_not_configured`: `.env` に `GOOGLE_MAPS_API_KEY` が無い
  - `503 geocode_auth_error`: `REQUEST_DENIED`（API 無効・キー制限など）
  - `503 geocode_rate_limited`: `OVER_QUERY_LIMIT` / `OVER_DAILY_LIMIT`
  - `404 geocode_not_found`: `ZERO_RESULTS`
  - `502 geocode_invalid_request` / `geocode_invalid_response` / `geocode_upstream_error`
  - `504 geocode_timeout`

```bash
curl -sS 'http://127.0.0.1:8000/api/reverse-geocode?lat=35.681236&lng=139.767125'
```

### `GET /api/backup` / `POST /api/backup`

PWA の設定タブ「データの手動同期」が使うバックアップAPIです。

- `POST /api/backup`
  - リクエストボディの JSON オブジェクトを `data/backup.json` に**そのまま**保存します。
  - 最大 10MB。空ボディ・不正 JSON・配列は `400`、10MB 超は `413`。
  - 成功レスポンス例: `{"ok": true, "bytes": 644, "savedAt": "2026-11-13T22:40:09+09:00"}`
- `GET /api/backup`
  - `data/backup.json` を返します。ファイルが無い場合は `{}`。
  - `Cache-Control: no-store` を付けます。

```bash
curl -sS http://127.0.0.1:8000/api/backup
curl -sS -X POST -H 'Content-Type: application/json' \
  --data-binary '{"drone-log:last-pilot":"PLT-0001"}' \
  http://127.0.0.1:8000/api/backup
```

- 保存先は環境変数 `DRONE_LOG_DATA_DIR`（既定: `./data`）で変更できます。
- `data/` は `.gitignore` 対象です。ディレクトリは `0700`、`backup.json` は `0600` で作成します。
- 操縦者名・技能証明番号・連絡先などの個人情報を含むため、BFF を一般公開しないでください。

### `GET /api/check-infrastructure`

指定座標から半径 `radius` メートル以内の `power=tower`（鉄塔）と
`power=line`（送電線）を数え、PWA のリスク判定用 JSON を返します。

- クエリパラメータ
  - `lat`（必須）: 緯度 `-90.0`〜`90.0`
  - `lon`（必須）: 経度 `-180.0`〜`180.0`
  - `radius`（任意）: 検索半径（m）。既定 `300`、`50`〜`2000`
- 成功時: `200` + 下記 JSON
- 入力不備: `422`（FastAPI 標準）
- 取得元の優先順
  - `radius` が `1000` 以下: 公式 OSM API（`/api/0.6/map`）を先に試し、失敗時のみ Overpass API
  - `radius` が `1000` 超: Overpass API を先に試し、失敗時のみ OSM API
- 混雑・障害時: `502` / `503` / `504` + エラーコード（例: `overpass_busy`、`osm_api_area_too_large`）
- 総処理時間が 9 秒を超えた場合は `504`（`infra_deadline_exceeded`）。
  応答本文が細切れに届く場合もチャンク読み取り中に deadline を監視して打ち切るため、
  クライアントが長時間待たされることはありません（PWA は 12 秒で打ち切り、無応答時は 1 回だけ自動再試行）。

```json
{
  "power_risk_detected": false,
  "towers_count": 0,
  "lines_count": 0,
  "message": "周辺インフラ（送電線・鉄塔）の異常なし",
  "source": "osm-api",
  "elapsed_ms": 1700
}
```

リスクがある場合は `power_risk_detected=true` になり、`message` に鉄塔数・
送電線数と飛行前の確認事項が入ります。PWA はこの値で赤（警告）／緑（安全）の
アラートを切り替え、`source` / `elapsed_ms` を「出典」として併記します。

```bash
curl -sS \
  'http://127.0.0.1:8000/api/check-infrastructure?lat=35.9000&lon=140.0000&radius=300'
```

Overpass API は混雑時に `406` / `429` / `502` / `503` / `504` を返すか、応答せずに
タイムアウトすることがあります（実測では `overpass-api.de` が 406/429、ミラーが
20 秒無応答になる時間帯がありました）。そのため本 BFF は次のように動作します。

1. 小半径（`1000m` 以下）は応答が速い OSM API を優先する（実測 1〜3 秒）。
2. Overpass へ問い合わせる場合は 接続3秒・応答5秒で打ち切り、タイムアウトは再試行しない。
   残り時間が `INFRA_DEADLINE_SECONDS` を下回っている場合は、その残り時間を上限にする。
3. Overpass の失敗が確定したら 300 秒間は Overpass を試さない（サーキットブレーカー）。
4. どちらも失敗した場合は、より具体的なエラー（`overpass_timeout` / `osm_api_area_too_large` など）を返す。

OSM API の bbox は半径から自動計算し、BFF 側で点と線分の距離をメートル単位で
評価します。ただし bbox 内の node が 5 万件を超えると OSM API は `400` を返すため、
市街地の `radius` 1500m 以上では Overpass が必要です。

ログには `source=overpass` / `source=osm-api` と `elapsed_ms` を出力します。
接続先は次の環境変数で変更できます。

| 環境変数 | 既定値 | 用途 |
|---|---|---|
| `OVERPASS_API_URLS` | `https://overpass-api.de/api/interpreter` | カンマ区切りでミラーを順に試す（旧 `OVERPASS_API_URL` も可） |
| `OSM_API_URL` | `https://api.openstreetmap.org/api/0.6/map` | OSM API の bbox 取得先 |

### `POST /api/parse-log`

- Content-Type: `multipart/form-data`
- フィールド名: `file`
- 許可する拡張子: `.txt`（大文字の `.TXT` も可）
- 成功時: `200` + 下記 JSON
- `.txt` 以外: `400` `{"detail": ".txt ファイルを選択してください。"}`
- `file` 未指定: `422`（FastAPI 標準）
- DJI ログとして解析できない `.txt`: `422` + 下記エラー詳細

成功レスポンスは PWA の `applyDjiLogToForm()` が期待するフラットな形です。
以下は `tests/sample_dji.txt`（実ログ v4）を解析した例です。

```json
{
  "fileName": "sample_dji.txt",
  "date": "2015-12-30",
  "takeoffTime": "04:05",
  "landingTime": "04:07",
  "durationMinutes": 2,
  "lat": 53.735879,
  "lng": -1.766518,
  "takeoffPlace": "53.735879, -1.766518"
}
```

`date` は離陸日（JST）です。`fileName` はアップロードされたファイルの
ベース名です。`lat` / `lng` は小数点以下 6 桁に丸め、`takeoffPlace` は
`"{lat:.6f}, {lng:.6f}"` 形式です。

解析できない場合、誤った飛行記録を防ぐためダミー値は返しません。

```json
{
  "detail": {
    "code": "dji_log_unreadable",
    "message": "DJIの飛行ログとして解析できませんでした。DJI Fly / DJI GO が出力した .txt ファイルを選択してください。"
  }
}
```

### curl 例

```bash
curl -sS -D - \
  -X POST \
  -F "file=@tests/sample_dji.txt;type=text/plain" \
  http://127.0.0.1:8000/api/parse-log
```

### `POST /api/export-log`

- Content-Type: `application/json`
- 成功時: `200` + Excel ファイル
  - `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
  - `Content-Disposition: attachment; filename="flight_record_{date}.xlsx"`
- 入力不備: `422`（FastAPI 標準）
- テンプレート欠落・生成失敗: `500` + エラーコード

リクエスト例:

```json
{
  "date": "2026-11-08",
  "operator_name": "普施 太郎",
  "registration_id": "JU1234567890",
  "purpose": "空撮",
  "location": "千葉県香取市小見川",
  "takeoff_time": "10:00",
  "landing_time": "10:15",
  "total_time": "15分",
  "safety_measures": "立入管理措置: 講じる / 飛行前点検: 全項目OK",
  "notes": "スモークテスト"
}
```

```bash
curl -sS -D flight_record_headers.txt \
  -X POST \
  -H "Content-Type: application/json" \
  --data-binary @tests/export_sample.json \
  http://127.0.0.1:8000/api/export-log \
  -o flight_record.xlsx
```

セル番地は国交省様式1「無人航空機の飛行記録」の公式テンプレートに合わせています。
帳票レイアウトを変更する場合は、`main.py` の書き込み先と
`template/flight_record_template.xlsx`、以下の対応表を合わせて更新してください。

| セル | 項目 | リクエストフィールド |
|---|---|---|
| B3 | 機体登録記号 | `registration_id` |
| B5 | 飛行年月日 | `date` |
| D5 | 操縦者氏名 | `operator_name` |
| G5 | 飛行概要 | `purpose` |
| H5 | 離陸場所 | `location` |
| I5 | 着陸場所 | `location`（離陸場所と同じ値） |
| L5 | 離陸時刻 | `takeoff_time` |
| M5 | 着陸時刻 | `landing_time` |
| O5 | 飛行時間 | `total_time` |
| Q5 | 安全措置・特記事項 | `safety_measures` / `notes`（` / ` で連結） |

`date` はファイル名にも使います。ファイル名に使えない文字は `_` に置換します。
Excel は `io.BytesIO` 上で生成し、レスポンス後はディスクに残りません。

`tests/export_sample.json` はスモークテストで使うサンプルです。手元で curl を
試す場合は、同じ内容の JSON ファイルを用意するか、`--data-binary` のパスを
`tests/export_sample.json` に合わせてください。

### `POST /api/diagnose`

飛行計画などから、Gemini による飛行前診断（プレーンテキスト）を生成します。
キーは `GEMINI_API_KEY` を使用します（[AI 飛行前診断（Gemini）の設定](#ai-飛行前診断geminiの設定)）。

リクエスト（`Content-Type: application/json`）:

```jsonc
{
  "source": "map",                 // conditions=周辺情報のみ / photo=現場写真も解析 / map=周辺地図も解析
  "flight": {
    "date": "2026-11-14",
    "takeoff_place": "千葉県香取市 小見川河川敷",
    "landing_place": "千葉県香取市 小見川河川敷",
    "lat": 35.9034, "lng": 140.5089,
    "takeoff_time": "09:00", "landing_time": "09:20",
    "duration_min": 20,
    "purpose": "橋梁点検の空撮",
    "specific_flags": ["DID", "150m", "目視外"],
    "has_clear_zone": true
  },
  "weather": {
    "text": "晴れ", "temperature": 18.5, "humidity": 55.0,
    "precipitation": 0.0, "wind_speed": 4.2, "wind_gust": 8.4,
    "safety_status": "CAUTION", "safety_label": "突風が強いため注意"
  },
  "elevation": { "ground_elevation": 5.2, "max_150m_limit": 155.2, "note": "国土地理院" },
  "regulations": { "is_did": true, "note": "DID内", "source": "国土地理院 DID2020" },
  "infrastructure": {
    "power_risk_detected": true, "towers_count": 2, "lines_count": 3,
    "message": "鉄塔2件・送電線3件", "source": "osm-api"
  },
  "drone": {
    "model_name": "DJI Mavic 3 Enterprise", "maker": "DJI",
    "weight_g": 920, "registration_no": "JU1234567890"
  },
  "pilot": {
    "license_type": "一等無人航空機操縦士",
    "license_endorsements": ["目視外飛行", "夜間飛行"]
  }
}
```

`source="photo"`（現場写真）または `source="map"`（周辺マップ）のときは
`image_base64`（data URI 可）と `image_mime_type`（`image/jpeg` / `image/png` /
`image/webp`）を指定します。写真は長辺 1200px、地図は 2 倍解像度の JPEG を想定し、
base64 で 8MB を超える入力は拒否します。`source="map"` では「地図から読み取れる
地形・道路・河川・建物密集・鉄塔/送電線」に触れ、人物・車両・距離は断定しないよう
プロンプトを切り替えます。

レスポンス:

```json
{
  "ok": true,
  "diagnosis": "・総合判断: ...\n・突風への対策: ...\n最終判断は操縦者が行います。",
  "model": "gemini-3.5-flash-lite",
  "source": "conditions",
  "generatedAt": "2026-11-14T18:13:29+09:00"
}
```

`diagnosis` はプレーンテキストです。PWA は `textContent` で描画し、Markdown や
HTML としては解釈しません。操縦者の氏名・連絡先は受け取らず、Gemini へも
送信しません（資格区分と限定解除のみ）。

エラーは FastAPI の `detail` に `{code, message}` を返します。

| ステータス | code | 意味 |
|---|---|---|
| 503 | `ai_not_configured` | `GEMINI_API_KEY` 未設定 |
| 503 | `ai_auth_error` | キーが無効（401/403） |
| 503 | `ai_model_not_found` | `GEMINI_MODEL` が利用不可（404） |
| 503 | `ai_rate_limited` | クォータ超過（429） |
| 502 | `ai_upstream_error` | Gemini 側の 5xx・想定外ステータス |
| 502 | `ai_invalid_request` | Gemini がリクエストを拒否（400） |
| 502 | `ai_invalid_response` | 応答が JSON でない |
| 502 | `ai_empty_response` | 生成テキストが空 |
| 504 | `ai_timeout` | 接続5秒・応答30秒を超過 |
| 422 | `image_required` | `source="photo"` / `"map"` なのに画像が無い |
| 422 | `invalid_image_type` | 未対応の MIME タイプ |
| 422 | `invalid_image_data` | base64 を復号できない |
| 422 | `ai_blocked` | 安全フィルタで生成がブロックされた |
| 413 | `image_too_large` | base64 が上限（8MB）超過 |

## 解析仕様

- **日時**: `Details.start_time`（UTC aware）を JST に変換。`date` は離陸日。
- **離着陸時刻**: 離陸 = `start_time` の秒以下を切り捨て。
  着陸 = `start_time + Details.total_time` の秒以下を切り捨て。
- **飛行時間**: 表示上の離着陸 HH:MM の差（分）。日跨ぎにも対応。
  同一分に収まる短時間飛行は 1 分として扱う。
- **座標**: フレームがある場合は「GPS 使用中かつ `gps_level >= 3`」の
  最初の有効座標。フレームがない場合は `Details.latitude/longitude`。
  `0, 0` や範囲外の座標は無効として扱い、有効な座標がなければ 422。
- **フレームのフォールバック**: `frames()` が空・例外・未対応形式でも、
  Details が読めれば解析を継続する。詳細は
  [`tests/fixtures/README.md`](tests/fixtures/README.md) を参照。

### 既知の制限

- `pydjirecord 1.3.0` は手元の v4 実ログで `frames()` が 0 件になる
  （レコードヘッダ未対応）。本 BFF は Details フォールバックで動作する。
- v7〜v12 の実ログでのフレーム解析は未検証（公開サンプルが見つからないため）。
- v13 以降のフレームは `DJI_API_KEY` がないと復号できない。Details は復号可能。
- ファイルサイズ上限・認証・レート制限は未実装。
- 周辺インフラ判定は Overpass / OSM API の可用性に依存する。Overpass は
  2026年時点で混雑が常態化しており、406/タイムアウトが発生する。本 BFF は
  OSM API へフォールバックするが、半径 2000m など広範囲では OSM API の
  取得上限（50,000 nodes）に達して失敗する場合がある。PWA は既定の 300m を使う。

## テスト

サーバーを起動した状態で、別ターミナルから実行します。

```bash
cd drone-log-bff
./tests/smoke_test.sh
```

以下を確認し、最後に `ALL TESTS PASSED` を表示します。

1. ヘルスチェック
2. 実ログ v4 の解析（Details フォールバック、JST 変換）
3. v14 最小ログの解析（暗号化 Details の復号）
4. `.csv` の拒否（400）
5. DJI ログでない `.txt` の安全側エラー（422 / `dji_log_unreadable`）
6. `file` 未指定（422）
7. CORS プリフライト（許可オリジンのエコー / 許可外オリジンの拒否）
8. 飛行日誌 Excel の生成（セル値・Content-Type・ファイル名）
9. 周辺インフラ API の入力バリデーション（緯度91 → 422）
10. AI 診断の入力バリデーション（`source=photo` / `source=map` で画像なし → 422 / `image_required`）
11. AI 診断の実呼び出し（Gemini / `conditions`）— `GEMINI_API_KEY` 未設定時は SKIP

`GET /api/check-infrastructure` の実データ取得は外部 API（Overpass / OSM API）へ
アクセスするため、`smoke_test.sh` には含めていません。手元では上記の curl 例で確認してください。
Overpass が混雑している場合は、OSM API フォールバックにより 20〜40 秒ほど
かかることがあります。

Gemini を呼ばないオフラインのユニットテストは `tests/test_diagnose.py` です
（`_request_gemini_text` をモックし、正常系・画像検証・エラー対応を確認）。

```bash
cd drone-log-bff
.venv/bin/python tests/test_diagnose.py -v
```

### バックアップAPIのテスト

`tests/backup_test.sh` は、本番の `data/backup.json` を壊さないよう
一時ディレクトリを `DRONE_LOG_DATA_DIR` に指定し、別ポート（既定: 8011）で
BFF を起動して検証します。

```bash
cd drone-log-bff
./tests/backup_test.sh
```

1. ファイル無しの `GET` → `{}`
2. `POST` → `200`、送信した生JSONと保存ファイルが一致
3. `GET` → `POST` したJSONと一致、`Cache-Control: no-store`
4. パーミッション（`data/` = 700、`backup.json` = 600）
5. 不正JSON・配列は `400`、既存バックアップは保持

最後に `ALL BACKUP TESTS PASSED` を表示します。

## CORS

`allow_credentials=False`（cookie を使わない）のうえで、以下のオリジンだけを許可します。

| 種別 | オリジン |
|---|---|
| 本番 PWA | `https://aromabird.github.io` |
| 本番 HTTPS（tailnet） | `https://mac-mini-m4.tail95f7a2.ts.net` |
| ローカル開発 | `http://localhost:<任意ポート>` / `http://127.0.0.1:<任意ポート>` / `http://[::1]:<任意ポート>` |
| Tailscale IP | `http://100.64.0.0/10` の任意 IP・任意ポート |

- `file://` で開いた場合（`Origin: null`）は許可されません。ローカル確認は
  `python3 -m http.server` を使ってください。
- 別の LAN IP（例: `http://192.168.1.20:8811`）から配信する場合は、
  `main.py` の `CORS_ALLOW_ORIGIN_REGEX` にそのオリジンを追加してください。
- 許可外オリジンのプリフライトは `400` を返し、`Access-Control-Allow-Origin` は付きません。
- **Chrome の Private Network Access (PNA)**: 公開 HTTPS の
  `https://aromabird.github.io` から Tailscale の `100.64.0.0/10` へ fetch すると、
  Chrome は「local address space」へのアクセスとして追加のプリフライトを送ります。
  本 BFF は `CORSMiddleware(allow_private_network=True)` により、許可オリジンへ
  `Access-Control-Allow-Private-Network: true` を返します（プリフライトは 200）。
  この設定が無いと PWA は `Failed to fetch` になります。
- Chrome 142 以降の **Local Network Access (LNA)** では、公開 HTTPS ページから
  ローカルネットワークへ接続する際に**ユーザーの許可プロンプト**が表示されます。
  ヘッダだけでは権限は付与されないため、初回は「許可」を選んでください。
  Safari や `http://localhost` のローカルプレビューではプロンプトは出ません。

## ファイル構成

```text
drone-log-bff/
├── main.py
├── requirements.txt
├── .gitignore
├── README.md
├── template/
│   └── flight_record_template.xlsx  # 国交省様式1の公式Excelテンプレート
├── data/                      # 実行時に自動作成（.gitignore / 0700）
│   └── backup.json            # PWAのlocalStorageバックアップ（0600）
└── tests/
    ├── sample_dji.txt          # 実ログ v4（Details フォールバック検証）
    ├── invalid_dji.txt         # 解析失敗の検証
    ├── export_sample.json      # export-log のリクエスト例
    ├── smoke_test.sh
    ├── backup_test.sh          # バックアップAPI（一時データディレクトリ）
    └── fixtures/
        ├── README.md           # 出典・ライセンス・期待値
        └── minimal_v14.txt     # v14 最小ログ（Details 復号検証）
```

## ライセンス表記

テスト用ログの出典とライセンスは
[`tests/fixtures/README.md`](tests/fixtures/README.md) に記載しています。
`pydjirecord` は MIT ライセンスです。

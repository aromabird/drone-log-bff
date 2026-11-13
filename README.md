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

## API

### `GET /api/health`

```json
{"status": "ok", "service": "drone-log-bff"}
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
- Overpass 混雑・障害: 公式 OSM API（`/api/0.6/map`）へ自動フォールバック
  - フォールバックも失敗した場合は `502` / `503` / `504` + エラーコード

```json
{
  "power_risk_detected": false,
  "towers_count": 0,
  "lines_count": 0,
  "message": "周辺インフラ（送電線・鉄塔）の異常なし"
}
```

リスクがある場合は `power_risk_detected=true` になり、`message` に鉄塔数・
送電線数と飛行前の確認事項が入ります。PWA はこの値で赤（警告）／緑（安全）の
アラートを切り替えます。

```bash
curl -sS \
  'http://127.0.0.1:8000/api/check-infrastructure?lat=35.9000&lon=140.0000&radius=300'
```

Overpass API は混雑時に `406` / `429` / `502` / `503` / `504` を返すことがあります。
本 BFF は短い間隔で 1 回再試行し、それでも失敗した場合に公式 OSM API の bbox を
使って同じ判定を継続します。OSM API の bbox は半径から自動計算し、BFF 側で
点と線分の距離をメートル単位で評価します。ログには `source=overpass` または
`source=osm-api` を出力します。接続先は環境変数 `OVERPASS_API_URL` /
`OSM_API_URL` で変更できます。

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

`GET /api/check-infrastructure` の実データ取得は外部 API（Overpass / OSM API）へ
アクセスするため、`smoke_test.sh` には含めていません。手元では上記の curl 例で確認してください。
Overpass が混雑している場合は、OSM API フォールバックにより 20〜40 秒ほど
かかることがあります。

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

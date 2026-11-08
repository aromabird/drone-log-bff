# drone-log-bff

ドローン飛行日誌 PWA（[`aromabird/drone-log`](https://github.com/aromabird/drone-log)）の
バックエンド・プロトタイプです。FastAPI で `.txt` の DJI ログを受け取り、
PWA の UI 開発用ダミー JSON を返します。

> **注意**: 現時点では実際のログ解析は行いません。どの `.txt` をアップロードしても
> 同じ固定値（10:00 離陸 / 10:15 着陸 / 15分 / 35.8988, 140.6385）を返します。

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

```bash
cd drone-log-bff
.venv/bin/uvicorn main:app --reload --port 8000
```

- API ドキュメント: <http://127.0.0.1:8000/docs>
- ヘルスチェック: <http://127.0.0.1:8000/api/health>

`uvicorn` の既定 bind は `127.0.0.1` です。iPhone など別端末から LAN 経由で
試す場合は `--host 0.0.0.0` を付け、macOS のファイアウォールを確認してください。

## API

### `GET /api/health`

```json
{"status": "ok", "service": "drone-log-bff"}
```

### `POST /api/parse-log`

- Content-Type: `multipart/form-data`
- フィールド名: `file`
- 許可する拡張子: `.txt`（大文字の `.TXT` も可）
- 成功時: `200` + 下記 JSON
- `.txt` 以外: `400` `{"detail": ".txt ファイルを選択してください。"}`
- `file` 未指定: `422`（FastAPI 標準）

成功レスポンスは PWA の `mockParseDjiLog()` と同じフラットな形です。

```json
{
  "fileName": "sample_dji.txt",
  "date": "2026-11-07",
  "takeoffTime": "10:00",
  "landingTime": "10:15",
  "durationMinutes": 15,
  "lat": 35.8988,
  "lng": 140.6385,
  "takeoffPlace": "35.8988, 140.6385"
}
```

`date` は JST（UTC+9）の実行日です。`fileName` はアップロードされた
ファイルのベース名です。

### curl 例

```bash
curl -sS -D - \
  -X POST \
  -F "file=@tests/sample_dji.txt;type=text/plain" \
  http://127.0.0.1:8000/api/parse-log
```

## テスト

サーバーを起動した状態で、別ターミナルから実行します。

```bash
cd drone-log-bff
./tests/smoke_test.sh
```

正常系、`.csv` の拒否、`file` 未指定、CORS プリフライトを確認し、
最後に `ALL TESTS PASSED` を表示します。

## CORS

プロトタイプでは `allow_origins=["*"]`、`allow_credentials=False` で全オリジンを
許可しています。PWA は cookie 認証を使わないため問題ありませんが、本番では
`https://aromabird.github.io` とローカル開発用オリジンに限定してください。

## 現在の制約

- 実解析ロジックは未実装（アップロード内容は読み捨て）
- ファイルをディスクへ保存しない
- 認証・レート制限・ファイルサイズ上限なし
- `--reload` と CORS ワイルドカードは開発用
- PWA 側は `parseDjiLogViaBFF()` で本 BFF に接続済み（既定 `http://127.0.0.1:8000/api/parse-log`）。
  公開 GitHub Pages から利用する場合は、環境別に BFF の URL を切り替える必要がある

## ファイル構成

```text
drone-log-bff/
├── main.py
├── requirements.txt
├── .gitignore
├── README.md
└── tests/
    ├── sample_dji.txt
    └── smoke_test.sh
```

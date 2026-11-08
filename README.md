# drone-log-bff

ドローン飛行日誌 PWA（[`aromabird/drone-log`](https://github.com/aromabird/drone-log)）の
バックエンドです。FastAPI で `.txt` の DJI ログを受け取り、
[`pydjirecord`](https://github.com/rembish/pydjirecord) でバイナリ解析して、
PWA がそのまま使える飛行サマリー JSON を返します。

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

```bash
cd drone-log-bff
.venv/bin/uvicorn main:app --reload --port 8000
```

- API ドキュメント: <http://127.0.0.1:8000/docs>
- ヘルスチェック: <http://127.0.0.1:8000/api/health>

`uvicorn` の既定 bind は `127.0.0.1` です。iPhone など別端末から LAN 経由で
試す場合は `--host 0.0.0.0` を付け、macOS のファイアウォールを確認してください。

### v13 以降のフレーム（GPS 軌跡）を使う場合

v13 以降のログはフレームが暗号化されており、DJI の API キーが必要です。
キーを設定しなくても Details（日時・座標・飛行時間）は復号できるため、
本 BFF のレスポンスには影響しません。フレームから最初の有効な GPS を
使いたい場合だけ、起動前に環境変数を設定してください。

```bash
export DJI_API_KEY="<DJI Developer API Key>"
.venv/bin/uvicorn main:app --reload --port 8000
```

キーは DJI のキーチェーン取得 API に送信されます。ログやレスポンスには
出力しません。未設定時は Details の座標を使います。

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
7. CORS プリフライト

## CORS

プロトタイプでは `allow_origins=["*"]`、`allow_credentials=False` で全オリジンを
許可しています。PWA は cookie 認証を使わないため問題ありませんが、本番では
`https://aromabird.github.io` とローカル開発用オリジンに限定してください。

## ファイル構成

```text
drone-log-bff/
├── main.py
├── requirements.txt
├── .gitignore
├── README.md
└── tests/
    ├── sample_dji.txt          # 実ログ v4（Details フォールバック検証）
    ├── invalid_dji.txt         # 解析失敗の検証
    ├── smoke_test.sh
    └── fixtures/
        ├── README.md           # 出典・ライセンス・期待値
        └── minimal_v14.txt     # v14 最小ログ（Details 復号検証）
```

## ライセンス表記

テスト用ログの出典とライセンスは
[`tests/fixtures/README.md`](tests/fixtures/README.md) に記載しています。
`pydjirecord` は MIT ライセンスです。

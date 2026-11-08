# DJI ログのテスト用フィクスチャ

`tests/smoke_test.sh` が使う入力ファイルの出典と期待値です。いずれも
アプリケーションの動作検証だけを目的としており、実在の飛行計画や個人情報を
含むものではありません（実ログは公開サンプル）。

## `tests/sample_dji.txt`

- 出典: [dji-log-parser](https://github.com/lvauvillier/dji-log-parser) の
  `examples/DJIFlightRecord_2015-12-29_[19-05-48].txt`
- ライセンス: MIT
- 内容: DJI Phantom 3 Pro / ログバージョン 4 / 2015-12-29 19:05:48 UTC
- 期待値（JST）: `date=2015-12-30`, `takeoffTime=04:05`,
  `landingTime=04:07`, `durationMinutes=2`,
  `lat=53.735879`, `lng=-1.766518`
- 補足: `pydjirecord 1.3.0` はこの v4 ログのレコードヘッダを解釈できず
  `frames()` が 0 件になる。そのため本ファイルは **Details フォールバック**
  （日時・座標・飛行時間）の検証に使う。

## `tests/fixtures/minimal_v14.txt`

- 出典: [pydjirecord](https://github.com/rembish/pydjirecord) の
  `tests/fixtures/minimal_v14.txt`
- ライセンス: MIT
- 内容: テスト用に生成された最小の v14 ログ（実飛行データではない）
- 期待値（JST）: `date=2021-05-26`, `takeoffTime=03:31`,
  `landingTime=03:36`, `durationMinutes=5`,
  `lat=41.3`, `lng=19.8`
- 補足: v13 以降のフレームは DJI の API キーが必要。キー未設定時の
  **Details 復号経路**を検証する。

## `tests/invalid_dji.txt`

- DJI ログではない単なるテキスト。HTTP 422 と
  `detail.code=dji_log_unreadable` を返す安全側フォールバックの検証に使う。

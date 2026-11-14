#!/usr/bin/env python3
"""OurAirports の公開データから日本の主要空港を抽出し `assets/airports_jp.json` を作る。

出典: OurAirports（https://ourairports.com/data/ , public domain）
      ミラー: https://davidmegginson.github.io/ourairports-data/airports.csv

使い方::

    cd drone-log-bff
    .venv/bin/python tools/build_airports_json.py

抽出条件は `iso_country=JP` かつ `type in (large_airport, medium_airport)`。
空港等の周辺判定（`legal.py`）はこのファイルをローカルで読むだけで、
実行時に外部へ問い合わせない。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from pathlib import Path

import requests

SOURCE_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
SOURCE_NOTE = (
    "OurAirports (https://ourairports.com/data/), public domain / "
    "ミラー: https://davidmegginson.github.io/ourairports-data/airports.csv"
)
TYPES = {"large_airport", "medium_airport"}

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "assets" / "airports_jp.json"

# 主要空港の日本語名（ICAO で引き当てる）。未登録は OurAirports の英語名を使う。
NAME_JA = {
    "RJAA": "成田国際空港",
    "RJTT": "東京国際空港（羽田）",
    "RJGG": "中部国際空港（セントレア）",
    "RJBB": "関西国際空港",
    "RJOO": "大阪国際空港（伊丹）",
    "RJBE": "神戸空港",
    "RJFF": "福岡空港",
    "RJCC": "新千歳空港",
    "ROAH": "那覇空港",
    "RJSS": "仙台空港",
    "RJSN": "新潟空港",
    "RJOA": "広島空港",
    "RJOM": "松山空港",
    "RJOT": "高松空港",
    "RJFT": "熊本空港",
    "RJFK": "鹿児島空港",
    "RJFU": "長崎空港",
    "RJFM": "宮崎空港",
    "RJFO": "大分空港",
    "RJOB": "岡山桃太郎空港",
    "RJNS": "静岡空港",
    "RJNK": "小松飛行場",
    "RJFR": "北九州空港",
    "ROIG": "新石垣空港",
    "ROMY": "宮古空港",
    "RJKA": "奄美空港",
    "RJCK": "釧路空港",
    "RJCB": "帯広空港（とかち）",
    "RJCH": "函館空港",
    "RJEC": "旭川空港",
    "RJSA": "青森空港",
    "RJSK": "秋田空港",
    "RJSI": "花巻空港",
    "RJSC": "山形空港",
    "RJSF": "福島空港",
    "RJAH": "茨城空港",
    "RJOK": "高知龍馬空港",
    "RJOS": "徳島阿波おどり空港",
    "RJOR": "鳥取砂丘コナン空港",
    "RJOH": "米子鬼太郎空港",
    "RJOC": "出雲縁結び空港",
    "RJDC": "山口宇部空港",
    "RJOI": "岩国錦帯橋空港",
    "RJFS": "佐賀空港",
    "RJDT": "対馬空港",
    "RJFE": "五島福江空港",
    "RJCW": "稚内空港",
    "RJEB": "オホーツク紋別空港",
    "RJCM": "女満別空港",
    "RJCN": "中標津空港",
    "RJER": "利尻空港",
    "RJTG": "東京ヘリポート",
    "RJNY": "静浜基地",
}


def main() -> int:
    response = requests.get(SOURCE_URL, timeout=(5, 30))
    response.raise_for_status()

    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
    airports = []
    for row in rows:
        if row.get("iso_country") != "JP":
            continue
        if row.get("type") not in TYPES:
            continue
        try:
            lat = float(row["latitude_deg"])
            lon = float(row["longitude_deg"])
        except (TypeError, ValueError):
            continue
        icao = (row.get("gps_code") or row.get("ident") or "").strip().upper()
        airports.append(
            {
                "name": NAME_JA.get(icao) or (row.get("name") or icao or "名称不明"),
                "icao": icao,
                "iata": (row.get("iata_code") or "").strip().upper(),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "type": row["type"],
            }
        )

    airports.sort(key=lambda a: (a["icao"], a["name"]))
    payload = {
        "source": SOURCE_NOTE,
        "generated_at": dt.date.today().isoformat(),
        "filter": "iso_country=JP / type in (large_airport, medium_airport)",
        "count": len(airports),
        "airports": airports,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT_PATH} ({len(airports)} airports, {OUT_PATH.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

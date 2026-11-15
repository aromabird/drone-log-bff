#!/usr/bin/env python3
"""launchd（LaunchAgent）の plist を生成して設置する。

`deploy/com.aromabird.drone-log-bff.plist.template` の `__REPO_DIR__` /
`__HOST__` / `__PORT__` を実値に置き換えて、
`~/Library/LaunchAgents/com.aromabird.drone-log-bff.plist` へ書き出します。
リポジトリには端末固有の絶対パスを保存しないため、clone した端末ごとに
一度実行してください。

使い方::

    cd drone-log-bff
    .venv/bin/python tools/install_launchd.py --dry-run   # 生成内容の確認のみ
    .venv/bin/python tools/install_launchd.py             # 設置（差分があれば --force が必要）
    .venv/bin/python tools/install_launchd.py --reload    # 設置して launchd を読み直す

`--reload` は BFF を再起動します（数秒のダウンタイム）。実行前に
`/api/health` が返ることを確認してください。生成物は plistlib で読み直して
検証するだけで、秘密情報の表示・保存は行いません。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

LABEL = "com.aromabird.drone-log-bff"
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "deploy" / f"{LABEL}.plist.template"
PLACEHOLDERS = ("__REPO_DIR__", "__HOST__", "__PORT__")


def render(host: str, port: int) -> str:
    """テンプレートのプレースホルダを実値へ置き換えた plist 文字列を返す。"""
    if not TEMPLATE.exists():
        raise SystemExit(f"テンプレートが見つかりません: {TEMPLATE}")
    text = TEMPLATE.read_text(encoding="utf-8")
    return (
        text.replace("__REPO_DIR__", escape(str(ROOT)))
        .replace("__HOST__", escape(host))
        .replace("__PORT__", str(port))
    )


def validate(text: str, port: int) -> None:
    """生成した plist が launchd の期待どおりかを検証する。"""
    try:
        data = plistlib.loads(text.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 例外種別は問わず中断する
        raise SystemExit(f"生成した plist が不正です: {exc}") from exc

    args = data.get("ProgramArguments") or []
    problems: list[str] = []
    if data.get("Label") != LABEL:
        problems.append("Label")
    if data.get("WorkingDirectory") != str(ROOT):
        problems.append("WorkingDirectory")
    if not args or args[0] != str(ROOT / ".venv" / "bin" / "uvicorn"):
        problems.append("ProgramArguments[0]")
    if args[-2:] != ["--port", str(port)]:
        problems.append("--port")
    problems.extend(token for token in PLACEHOLDERS if token in text)
    if problems:
        raise SystemExit("plist の検証に失敗しました: " + ", ".join(problems))


def _wait_running(target: str, timeout: float = 5.0) -> bool:
    """launchctl の状態が `running` になるまで待つ。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
        if res.returncode == 0 and "state = running" in res.stdout:
            return True
        time.sleep(0.25)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="launchd の plist を生成・設置する")
    parser.add_argument("--host", default="0.0.0.0", help="待ち受けアドレス（既定 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8000, help="待ち受けポート（既定 8000）")
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist",
        help="出力先（既定 ~/Library/LaunchAgents/<label>.plist）",
    )
    parser.add_argument("--dry-run", action="store_true", help="書き込まず生成結果だけ表示する")
    parser.add_argument(
        "--force",
        action="store_true",
        help="既存の plist が違っても上書きする（.bak-YYYYmmdd-HHMMSS を作成）",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="設置後に launchctl で読み直す（BFF が再起動する）",
    )
    args = parser.parse_args()

    text = render(args.host, args.port)
    validate(text, args.port)

    uvicorn = ROOT / ".venv" / "bin" / "uvicorn"
    if not uvicorn.exists():
        print(f"⚠️  {uvicorn} がありません。先に .venv を作成してください。", file=sys.stderr)

    print(f"repo    : {ROOT}")
    print(f"dest    : {args.dest}")
    print(f"listen  : {args.host}:{args.port}")

    if args.dry_run:
        print("--dry-run: 書き込みません。生成される plist:")
        print(text)
        return 0

    if args.dest.exists() and args.dest.read_text(encoding="utf-8") != text:
        if not args.force:
            print(
                "既存の plist と内容が違います。--force を付けると上書きします"
                "（.bak-YYYYmmdd-HHMMSS を作成）。",
                file=sys.stderr,
            )
            return 1
        backup = args.dest.with_name(f"{args.dest.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(args.dest, backup)
        print(f"backup  : {backup}")

    args.dest.parent.mkdir(parents=True, exist_ok=True)
    args.dest.write_text(text, encoding="utf-8")
    args.dest.chmod(0o644)
    print("wrote   : ok")

    if not args.reload:
        print(f"次の手順: launchctl bootstrap gui/$(id -u) {args.dest}  （または --reload で実行）")
        return 0

    target = f"gui/{os.getuid()}/{LABEL}"
    subprocess.run(["launchctl", "bootout", target], capture_output=True, check=False)

    # `bootout` は非同期に完了するため、直後の `bootstrap` が
    # "Bootstrap failed: 5: Input/output error" で失敗することがある
    # （2026-11-15 実測）。少し待って数回まで再試行する。
    res = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(args.dest)],
        capture_output=True,
        text=True,
    )
    delay = 0.5
    for _ in range(3):
        if res.returncode == 0:
            break
        time.sleep(delay)
        delay *= 2
        res = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(args.dest)],
            capture_output=True,
            text=True,
        )
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        print(f"launchctl bootstrap 失敗: {detail}", file=sys.stderr)
        print(
            "手動で再実行してください: "
            f"launchctl bootstrap gui/$(id -u) {args.dest}",
            file=sys.stderr,
        )
        return res.returncode
    if not _wait_running(target):
        print(
            f"⚠️  {target} が running になりません。`launchctl print {target}` を確認してください。",
            file=sys.stderr,
        )
        return 1
    print(f"reload  : {target}（state = running）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

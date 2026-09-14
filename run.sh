#!/usr/bin/env bash
# adb_screenshot を仮想環境 (.venv) 上で起動する。
#   ./run.sh                 GUI
#   ./run.sh --no-gui -n 10  CLI（引数はそのまま main.py へ渡す）
# 初回は .venv を作成して requirements.txt をインストールする。
# requirements.txt が更新された場合も自動で再インストールする。
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

PYTHON="${PYTHON:-python3}"
VENV=".venv"
REQ="requirements.txt"
STAMP="$VENV/.requirements.stamp"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON が見つかりません" >&2
    exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
    echo "[run.sh] 仮想環境を作成します: $VENV"
    "$PYTHON" -m venv --system-site-packages "$VENV"
fi

if [ ! -f "$STAMP" ] || [ "$REQ" -nt "$STAMP" ]; then
    echo "[run.sh] 依存パッケージをインストールします ($REQ)"
    "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
    "$VENV/bin/python" -m pip install -r "$REQ"
    touch "$STAMP"
fi

exec "$VENV/bin/python" main.py "$@"

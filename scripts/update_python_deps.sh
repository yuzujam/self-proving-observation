#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── update_python_deps.sh  — proposed-node上のホスト用.venvのPython依存
#         パッケージを定期的に最新へ更新する（cron: 毎週日曜03:30、proposed-nodeのみ）。
#
# 対象はFidelity Guard・measure/ml等のホスト側解析スクリプトが使う.venvのみ。
# receiver/workerはDockerコンテナ内の別のPython環境（proposed/receiver/requirements.txt
# ベース）を使っており本スクリプトの対象外。観測パイプライン自体は無停止・無影響。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

mkdir -p logs
LOG_FILE="logs/update_python_deps.log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

VENV_PIP=".venv/bin/pip"
VENV_PYTHON=".venv/bin/python3"

if [ ! -x "$VENV_PIP" ]; then
    log "[ERROR] $VENV_PIP が見つかりません。中断します。"
    exit 1
fi

SNAPSHOT="logs/pip_freeze_$(date '+%Y%m%d_%H%M%S').txt"
"$VENV_PIP" freeze > "$SNAPSHOT"
log "更新前のパッケージ一覧を $SNAPSHOT に保存しました"

if "$VENV_PIP" install --upgrade -e ".[dev]" >> "$LOG_FILE" 2>&1; then
    log "pip install --upgrade -e .[dev] 成功"
else
    log "[ERROR] pip install --upgrade -e .[dev] 失敗。ロールバックは $SNAPSHOT を参照し 'pip install -r $SNAPSHOT' で可能"
    exit 1
fi

if "$VENV_PYTHON" -c "import fastapi, redis, torch, shap, numpy, scipy, matplotlib, psutil" 2>>"$LOG_FILE"; then
    log "更新後の主要モジュールimport確認: OK"
else
    log "[ERROR] 更新後のimport確認に失敗。$SNAPSHOT からのロールバックを検討してください"
    exit 1
fi

log "update_python_deps.sh 完了"

#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── run_fidelity_cron.sh  — Fidelity Guard 実験を cron から定期実行するラッパー
#
# run_cron.sh（対照実験バッチ用）との違い:
#   - Fidelity Guard はローカルの合成データのみで動作し、baseline/proposed
#     ノードへの接続を必要としない（ヘルスチェック不要）
#   - 目的は対照実験のような固定回数の完了そのものではなく、観測期間中に
#     試行数を積み増して検知率の統計的信頼区間を狭めること。
#     recurring シナリオで n=3→n=10 に増やしただけで confirming_detected
#     の95%信頼区間が [20.8%, 93.9%] → [59.6%, 98.2%] に大きく縮んだことを
#     実測で確認済み（n=3 では「検知しにくい」と「ただのノイズ」の区別が
#     つかなかった）。cron 実行を重ねて全シナリオの n を継続的に増やす。
#
# crontab 設定例（毎週日曜 05:00 に起動。1回あたり trials=10 ×
# 3シナリオ=30試行で1時間前後かかりうるため、02:00 の
# backup_clickhouse.sh（毎日）と時間帯が重ならないよう間隔を空けている。
# 同時刻に重なるとClickHouseエクスポート/rcloneアップロードとLSTM学習+SHAP
# 計算のCPU・ディスクI/Oが競合し、本番の受付パイプラインに影響しうる）:
#   0 5 * * 0 cd ~/self-proving-observation && bash scripts/run_fidelity_cron.sh >> logs/fidelity_cron.log 2>&1
#
# 追加実験したい場合:
#   experiment.env の MAX_FIDELITY_RUNS を増やし、
#   results/fidelity/.experiment_done を削除するだけ
#
# 完全停止したい場合:
#   crontab -e で該当行を手動削除
#
# 手動実行:
#   bash scripts/run_fidelity_cron.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"

# ── 設定ファイル読み込み ──
load_experiment_env

# ── パラメータ ──
MAX_FIDELITY_RUNS="${MAX_FIDELITY_RUNS:-12}"   # 週1回想定で約90日分
FIDELITY_TRIALS="${FIDELITY_TRIALS:-10}"       # シナリオごとの試行数（信頼区間確保のため既定n=10）
FIDELITY_EPOCHS="${FIDELITY_EPOCHS:-50}"
FIDELITY_SEED_BASE="${FIDELITY_SEED_BASE:-42}" # run_fidelity_experiment.pyのseed既定値と揃える
LOCK_FILE="/tmp/obs_fidelity_experiment.lock"
RESULTS_DIR="$PROJECT_ROOT/results/fidelity"
STATE_FILE="$RESULTS_DIR/.cron_state"
DONE_FILE="$RESULTS_DIR/.experiment_done"
mkdir -p "$RESULTS_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ── ロックチェック ──
# PIDファイル+kill -0によるcheck-then-act方式は、(a) チェックとロック取得の
# 間に排他がなく手動実行とcronの同時起動をすり抜ける、(b) 異常終了して
# ロックが残った場合、そのPIDが後に別プロセスへ再利用されると「実行中」と
# 誤判定し以後の週次実行が永久にスキップされ続ける、という2つの競合状態を
# 持っていた。run_full_thesis.sh（doc/known-limitations.md #H）と同じ
# flock -n によるアトミックなロックに統一する（プロセス終了時にfd経由で
# 自動解放されるため、ロックファイルの手動削除も不要）。
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "[SKIP] 実験中（別プロセスがロックを保持）。今回はスキップします。"
    exit 0
fi

# ── 実行回数チェック ──
COMPLETED_RUNS=0
if [ -f "$STATE_FILE" ]; then
    COMPLETED_RUNS=$(grep "^completed=" "$STATE_FILE" 2>/dev/null | cut -d= -f2 || echo "0")
fi

if [ -f "$DONE_FILE" ]; then
    DONE_RUNS=$(grep "^completed=" "$DONE_FILE" 2>/dev/null | cut -d= -f2 || echo "?")
    log "[SKIP] 実験完了済み (${DONE_RUNS}/${MAX_FIDELITY_RUNS} 回)。追加するには MAX_FIDELITY_RUNS を増やして .experiment_done を削除してください。"
    exit 0
fi

if [ "$COMPLETED_RUNS" -ge "$MAX_FIDELITY_RUNS" ]; then
    log "[DONE] 規定回数 ($MAX_FIDELITY_RUNS 回) に達しました。"
    cp "$STATE_FILE" "$DONE_FILE"
    exit 0
fi

NEXT_RUN=$((COMPLETED_RUNS + 1))
BATCH_DIR="$RESULTS_DIR/cron_run$(printf '%02d' "$NEXT_RUN")_$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${BATCH_DIR}.log"
# trial_seed = seed + trial（run_fidelity_experiment.py）はtrial(1..FIDELITY_TRIALS)にしか
# 依存しないため、run間でseedをずらさないと毎回同じ乱数列＝同じ結果を再生するだけになる
# （cron_run01・02が数値まで完全一致していたことで発覚）。run番号ごとに重ならない
# ブロックを割り当てる。
FIDELITY_SEED=$((FIDELITY_SEED_BASE + (NEXT_RUN - 1) * FIDELITY_TRIALS))

log "============================================"
log " Fidelity Guard 定期実験 ${NEXT_RUN}/${MAX_FIDELITY_RUNS} 回目"
log " ログ: $LOG_FILE"
log "============================================"

set +e
python3 "$PROJECT_ROOT/scripts/run_fidelity_experiment.py" \
    --output-dir "$BATCH_DIR" \
    --trials "$FIDELITY_TRIALS" \
    --epochs "$FIDELITY_EPOCHS" \
    --seed "$FIDELITY_SEED" \
    > "$LOG_FILE" 2>&1
EXIT_CODE=$?
set -e

if [ "$EXIT_CODE" -eq 0 ]; then
    printf "completed=%s\nlast_run=%s\n" "$NEXT_RUN" "$(date '+%Y-%m-%d %H:%M:%S')" > "$STATE_FILE"
else
    log "[OBS FAILED] Fidelity Guard 実験 ${NEXT_RUN}/${MAX_FIDELITY_RUNS} 失敗 (exit=$EXIT_CODE)"
    log "最後のログ:"
    tail -20 "$LOG_FILE" 2>/dev/null | while IFS= read -r line; do log "$line"; done
fi

log "[DONE] Fidelity Guard 実験 ${NEXT_RUN}/${MAX_FIDELITY_RUNS} 終了 (exit=$EXIT_CODE)"

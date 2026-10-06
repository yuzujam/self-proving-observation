#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── run_cron.sh  — cron から安全に呼ぶラッパー
#
# 機能:
#   - ロックファイルで二重起動を防止
#   - 実行回数を results/.cron_state で管理（crontab は一切触らない）
#   - MAX_RUNS 到達時は results/.experiment_done を作成してスキップ
#   - nohup で run_full_thesis.sh --skip-infra をバックグラウンド起動
#   - 開始・完了・失敗を通知（ログ出力、メール送信は撤去済み）
#
# crontab（実運用は scripts/expected-crontab/proposed.txt、4時間ごとに起動）:
#   0 */4 * * * cd /home/<user>/self-proving-observation && bash scripts/run_cron.sh >> logs/cron_master.log 2>&1
# 実験中はロックで、規定回数の完了後は results/.experiment_done でスキップされるため、
# 起動頻度が高くても多重実行・超過実行にはならない。
#
# 追加実験したい場合:
#   experiment.env の MAX_RUNS を増やし、results/.experiment_done を削除するだけ
#
# 完全停止したい場合:
#   crontab -e で該当行を手動削除
#
# 手動実行:
#   bash scripts/run_cron.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# ── 起動記録（「行の不在」による検知用、doc/known-limitations.md #S・#PP） ──
# ロック・実行回数チェックより前に書く: これらのチェックでスキップ・失敗する
# ケースも含め「cronがこのスクリプトを起動したこと」自体を記録する。
# 2026-07-07に発生した「run_cron.sh自体が起動した形跡がない」原因不明の
# 空白（#S）は、起動有無を示す記録が一切存在しなかったため事後調査もできな
# かった。UTCで記録するのはClickHouseセッションTZ（UTC）・分析スクリプトとの
# 時刻基準統一のため（ホストのCESTとは別、内部指針 3.2参照）。
mkdir -p "$PROJECT_ROOT/logs"
date -u '+%Y-%m-%d %H:%M:%S' >> "$PROJECT_ROOT/logs/run_cron_invocations.log"

source "$SCRIPT_DIR/lib/common.sh"

# ── 設定ファイル読み込み ──
load_experiment_env

# ── パラメータ ──
MAX_RUNS="${MAX_RUNS:-5}"                              # 論文用: 5回（2夜失敗しても30試行確保）
LOCK_FILE="/tmp/obs_experiment.lock"
STATE_FILE="$PROJECT_ROOT/results/.cron_state"
DONE_FILE="$PROJECT_ROOT/results/.experiment_done"
mkdir -p "$PROJECT_ROOT/results"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ── ロックチェック ──
if [ -e "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        log "[SKIP] 実験中 (PID=$LOCK_PID)。今回はスキップします。"
        exit 0
    else
        log "[WARN] 古いロックファイルを削除します (PID=$LOCK_PID は終了済み)"
        rm -f "$LOCK_FILE"
    fi
fi

# ── 実行回数チェック ──
COMPLETED_RUNS=0
if [ -f "$STATE_FILE" ]; then
    COMPLETED_RUNS=$(grep "^completed=" "$STATE_FILE" 2>/dev/null | cut -d= -f2 || echo "0")
fi

# ── 完了マーカーチェック ──
if [ -f "$DONE_FILE" ]; then
    DONE_RUNS=$(grep "^completed=" "$DONE_FILE" 2>/dev/null | cut -d= -f2 || echo "?")
    log "[SKIP] 実験完了済み (${DONE_RUNS}/${MAX_RUNS} 回)。追加するには MAX_RUNS を増やして .experiment_done を削除してください。"
    exit 0
fi

if [ "$COMPLETED_RUNS" -ge "$MAX_RUNS" ]; then
    log "[DONE] 規定回数 ($MAX_RUNS 回) に達しました。"
    cp "$STATE_FILE" "$DONE_FILE"
    exit 0
fi

NEXT_RUN=$((COMPLETED_RUNS + 1))
log "============================================"
log " 定期実験 ${NEXT_RUN}/${MAX_RUNS} 回目"
log "============================================"

# ── ロック取得（実際の PID は nohup 後に書き込む） ──
echo "$$" > "$LOCK_FILE"

# ── ヘルスチェック（起動前に両ノードが生きているか確認） ──
BASELINE_ES="${ELASTICSEARCH_URL:-}"
PROPOSED_HEALTH="${PROPOSED_URL:-}"
PROPOSED_HEALTH_URL="${PROPOSED_HEALTH%/events}/health"

log "[CHECK] ベースライン ($BASELINE_ES)..."
if ! health_ok "${BASELINE_ES}/_cluster/health"; then
    log "[ERROR] ベースラインノードに接続できません。スキップします。"
    rm -f "$LOCK_FILE"
    exit 1
fi
log "  OK"

log "[CHECK] 提案手法ノード ($PROPOSED_HEALTH_URL)..."
if ! health_ok "$PROPOSED_HEALTH_URL"; then
    log "[ERROR] 提案手法ノードに接続できません。スキップします。"
    rm -f "$LOCK_FILE"
    exit 1
fi
log "  OK"

# ── バックグラウンド実行 + 完了監視 ──
# nohup と wait を同一サブシェル内で行うことで "not a child of this shell" を回避する
LOG_FILE="$PROJECT_ROOT/results/cron_run${NEXT_RUN}_$(date +%Y%m%d_%H%M%S).log"
log "[START] バックグラウンド起動 → $LOG_FILE"

log "[OBS START] 実験開始: ${NEXT_RUN}/${MAX_RUNS}（ログ: $LOG_FILE）"

(
    nohup bash "$SCRIPT_DIR/run_full_thesis.sh" --skip-infra > "$LOG_FILE" 2>&1 &
    INNER_PID=$!
    echo "$INNER_PID" > "$LOCK_FILE"

    wait "$INNER_PID"
    EXIT_CODE=$?

    if [ "$EXIT_CODE" -eq 0 ]; then
        printf "completed=%s\nlast_run=%s\n" "$NEXT_RUN" "$(date '+%Y-%m-%d %H:%M:%S')" > "$STATE_FILE"
        rm -f "$LOCK_FILE"
        log "[OBS DONE] 実験完了: ${NEXT_RUN}/${MAX_RUNS}（ログ: $LOG_FILE）"
    else
        rm -f "$LOCK_FILE"
        log "[OBS FAILED] 実験 ${NEXT_RUN}/${MAX_RUNS} 失敗 (exit=$EXIT_CODE、ログ: $LOG_FILE)"
        log "最後のログ:"
        tail -20 "$LOG_FILE" 2>/dev/null | while IFS= read -r line; do log "$line"; done
    fi
) &
MONITOR_PID=$!
log "  Monitor PID: $MONITOR_PID"

log "[INFO] 完了監視をバックグラウンドに委譲しました。cron プロセスはここで終了します。"
log "  ログ確認: tail -f $LOG_FILE"

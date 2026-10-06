#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── run_experiment.sh  — 対照実験の全自動オーケストレーション

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"

# ── 設定ファイル読み込み ──
load_experiment_env

_EXIT_CODE=0
_INTERRUPTED=false
MON_BASELINE_PID=""
MON_PROPOSED_PID=""
cleanup() {
    kill "${MON_BASELINE_PID:-}" "${MON_PROPOSED_PID:-}" 2>/dev/null || true
    wait "${MON_BASELINE_PID:-}" "${MON_PROPOSED_PID:-}" 2>/dev/null || true
    if [ "$_INTERRUPTED" = true ] || [ "$_EXIT_CODE" -ne 0 ]; then
        local reason="終了コード: $_EXIT_CODE"
        [ "$_INTERRUPTED" = true ] && reason="Ctrl+C または SIGTERM により中断"
        echo "[OBS FAILED] 実験が失敗しました: ${TIMESTAMP:-unknown}（理由: ${reason}、結果ディレクトリ: ${RESULTS_DIR:-N/A}）"
    fi
}
trap '_EXIT_CODE=$?; cleanup' EXIT
trap '_INTERRUPTED=true; exit 130' INT TERM

# 環境変数（上書き可能）
BASELINE_URL="${BASELINE_URL:-http://localhost:5080}"
PROPOSED_URL="${PROPOSED_URL:-http://localhost:8000/events}"
CLICKHOUSE_URL="${CLICKHOUSE_URL:-http://localhost:8123}"
ELASTICSEARCH_URL="${ELASTICSEARCH_URL:-http://localhost:9200}"
RPS="${RPS:-100}"
DURATION="${DURATION:-60}"
PATTERN="${PATTERN:-spike}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_DIR="$PROJECT_ROOT/results/$TIMESTAMP"
mkdir -p "$RESULTS_DIR"

echo "============================================"
echo " 対照実験"
echo " $TIMESTAMP"
echo " パターン: ${PATTERN}  RPS: ${RPS}  持続時間: ${DURATION}s"
echo " 結果ディレクトリ: ${RESULTS_DIR}"
echo "============================================"
echo ""

# --- Step 1: ヘルスチェック ---
echo "[1/6] ヘルスチェック..."
health_ok "${PROPOSED_URL%/events}/health" || { echo "[ERROR] Proposed node not reachable"; exit 1; }
echo "  Proposed: OK"
health_ok "$ELASTICSEARCH_URL/_cluster/health" || { echo "[ERROR] Baseline Elasticsearch not reachable"; exit 1; }
echo "  Baseline Elasticsearch: OK"
health_ok "$BASELINE_URL" \
    && echo "  Baseline Logstash: OK" \
    || echo "  [WARN] Baseline Logstash ($BASELINE_URL) not reachable — baseline injection will show sent_ok=0"
echo ""

# --- Step 2: リソースモニター起動 ---
echo "[2/6] リソースモニター起動..."

_ssh_args_baseline=()
if [ -n "${BASELINE_IP:-}" ]; then
    _ssh_args_baseline+=(--ssh-host "${BASELINE_IP}" --ssh-user "${BASELINE_SSH_USER:-root}" --ssh-port "${BASELINE_SSH_PORT:-22}")
fi

_ssh_args_proposed=()
if [ -n "${PROPOSED_IP:-}" ]; then
    _ssh_args_proposed+=(--ssh-host "${PROPOSED_IP}" --ssh-user "${PROPOSED_SSH_USER:-root}" --ssh-port "${PROPOSED_SSH_PORT:-22}")
fi

python3 "$PROJECT_ROOT/src/monitor/resource.py" \
    --compose-file "$PROJECT_ROOT/baseline/docker-compose.yml" \
    "${_ssh_args_baseline[@]}" \
    --output "$RESULTS_DIR/resource_baseline.csv" \
    --duration $((DURATION + 30)) &
MON_BASELINE_PID=$!

python3 "$PROJECT_ROOT/src/monitor/resource.py" \
    --compose-file "$PROJECT_ROOT/proposed/docker-compose.yml" \
    "${_ssh_args_proposed[@]}" \
    --output "$RESULTS_DIR/resource_proposed.csv" \
    --duration $((DURATION + 30)) &
MON_PROPOSED_PID=$!
echo "  Baseline monitor PID: $MON_BASELINE_PID (mode: ${BASELINE_IP:-local})"
echo "  Proposed monitor PID: $MON_PROPOSED_PID (mode: ${PROPOSED_IP:-local})"
echo ""

# --- Step 3: 攻撃ジェネレーター実行 ---
echo "[3/6] 攻撃ジェネレーター実行 (rps=$RPS, duration=${DURATION}s, pattern=$PATTERN)..."
python3 "$PROJECT_ROOT/src/generator/spike.py" \
    --targets "$BASELINE_URL" "$PROPOSED_URL" \
    --rps "$RPS" \
    --duration "$DURATION" \
    --pattern "$PATTERN" \
    --output "$RESULTS_DIR/inject_log.csv"
echo ""

# --- Step 4: データ伝搬待機 — Redisキューが空になるまでポーリング（最大30分） ---
echo "[4/6] データ伝搬待機..."
MAX_WAIT=1800
waited=0
while [ $waited -lt $MAX_WAIT ]; do
    queue_len=$(queue_length "${PROPOSED_URL%/events}/health")
    if [ "$queue_len" = "0" ]; then
        echo "  キュー空 (${waited}s) → ClickHouse書き込みバッファ 5s"
        sleep 5
        break
    fi
    if [ $(( waited % 30 )) -eq 0 ]; then
        echo "  キュー残: ${queue_len}件 (${waited}s経過)..."
    fi
    sleep 5
    waited=$(( waited + 5 ))
done
if [ $waited -ge $MAX_WAIT ]; then
    echo "  [WARN] ${MAX_WAIT}s タイムアウト。キューに残件あり。欠損率に影響する可能性があります。"
fi

# --- Step 5: 欠損率計測 ---
echo "[5/6] 欠損率計測..."
python3 "$PROJECT_ROOT/src/measure/loss_rate.py" \
    --inject-log "$RESULTS_DIR/inject_log.csv" \
    --clickhouse "$CLICKHOUSE_URL" \
    --elasticsearch "$ELASTICSEARCH_URL" \
    --proposed-target "$PROPOSED_URL" \
    --baseline-target "$BASELINE_URL" \
    --output "$RESULTS_DIR/loss_rate.json"
echo ""

# --- Step 6: リソースモニター停止 & 可視化 ---
echo "[6/6] モニター停止 & レポート生成..."
kill "$MON_BASELINE_PID" "$MON_PROPOSED_PID" 2>/dev/null || true
wait "$MON_BASELINE_PID" "$MON_PROPOSED_PID" 2>/dev/null || true

echo ""
echo "============================================"
echo " 実験完了: $RESULTS_DIR"
echo "============================================"
echo ""
echo "結果ファイル:"
ls -la "$RESULTS_DIR/"
echo ""
echo "可視化コマンド:"
echo "  python3 src/visualize/report.py --results-dir $RESULTS_DIR"

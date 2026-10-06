#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── run_batch_resumable.sh  — レジューム機能付きバッチ対照実験
#
# 使い方:
#   bash scripts/run_batch_resumable.sh                        # 新規実行
#   BATCH_DIR=results/batch_XXXXX bash scripts/run_batch_resumable.sh  # 中断再開
#
# 4パターン × RPSレベル × 試行回数のバッチ対照実験を行い、
# 各試行の loss_rate.json が既に存在する場合はスキップする（レジューム対応）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"

# ── 設定ファイル読み込み ──
load_experiment_env verbose

# ── 実験パラメータ（環境変数で上書き可能） ──
PATTERNS=(flat spike wave ramp)
RPS_LEVELS=(100 500 1000 2000 5000)
TRIALS="${TRIALS:-5}"
DURATION="${DURATION:-120}"
COOLDOWN="${COOLDOWN:-30}"

BASELINE_URL="${BASELINE_URL:-http://localhost:5080}"
PROPOSED_URL="${PROPOSED_URL:-http://localhost:8000/events}"
CLICKHOUSE_URL="${CLICKHOUSE_URL:-http://localhost:8123}"
ELASTICSEARCH_URL="${ELASTICSEARCH_URL:-http://localhost:9200}"

# ── 出力ディレクトリ（BATCH_DIR 指定でレジューム） ──
if [ -n "${BATCH_DIR:-}" ] && [ -d "$BATCH_DIR" ]; then
    echo "[RESUME] 既存バッチディレクトリを使用: $BATCH_DIR"
else
    BATCH_ID=$(date +%Y%m%d_%H%M%S)
    BATCH_DIR="$PROJECT_ROOT/results/batch_$BATCH_ID"
    mkdir -p "$BATCH_DIR"
    echo "[NEW] 新規バッチ: $BATCH_DIR"
fi

total_runs=$(( ${#PATTERNS[@]} * ${#RPS_LEVELS[@]} * TRIALS ))
current_run=0
skipped=0
failed=0

_EXIT_CODE=0
_INTERRUPTED=false
MON_B=""
MON_P=""
cleanup() {
    kill "${MON_B:-}" "${MON_P:-}" 2>/dev/null || true
    wait "${MON_B:-}" "${MON_P:-}" 2>/dev/null || true
    if [ "$_INTERRUPTED" = true ] || [ "$_EXIT_CODE" -ne 0 ]; then
        local reason="終了コード: ${_EXIT_CODE}"
        [ "$_INTERRUPTED" = true ] && reason="Ctrl+C または SIGTERM により中断（再開: BATCH_DIR=${BATCH_DIR} bash scripts/run_batch_resumable.sh）"
        echo "[OBS FAILED] バッチ実験が失敗しました: ${BATCH_DIR##*/}（理由: ${reason}、進捗: ${current_run}/${total_runs} 回、スキップ: ${skipped}、結果: ${BATCH_DIR}）"
    fi
}
trap '_EXIT_CODE=$?; cleanup' EXIT
trap '_INTERRUPTED=true; exit 130' INT TERM

echo "============================================"
echo " バッチ対照実験（レジューム対応）"
echo "============================================"
echo " パターン: ${PATTERNS[*]}"
echo " RPS:      ${RPS_LEVELS[*]}"
echo " 試行回数: $TRIALS"
echo " 持続秒数: ${DURATION}s"
echo " 合計:     $total_runs 回"
echo " 出力先:   $BATCH_DIR"
echo "============================================"
echo ""

# ── ヘルスチェック ──
echo "[INIT] ヘルスチェック..."
health_ok "${PROPOSED_URL%/events}/health" \
    || { echo "[ERROR] Proposed (${PROPOSED_URL}) に接続できません"; exit 1; }
echo "  Proposed: OK"

BASELINE_ES="${ELASTICSEARCH_URL}"
health_ok "$BASELINE_ES/_cluster/health" \
    || { echo "[ERROR] Baseline ($BASELINE_ES) に接続できません"; exit 1; }
echo "  Baseline: OK"
echo ""


# 1トライアル分の実処理（リソースモニター起動→生成器→ドレイン待機→欠損率計測）。
# set -e は関数内にも及ぶため、ここで起きた失敗はこの関数の return 1 で
# 止まるだけで済み、呼び出し元の "if run_trial ...; then" が吸収する。
# 関数化する前は生成器・欠損率計測の失敗がスクリプト全体を即座に終了させ、
# ネットワーク瞬断1件で200試行中の残り全部を巻き添えにしていた
# （2026-07-13、ディスク逼迫の緊急対応と重なりSSH接続断・ClickHouse/ES失敗が
# 連鎖し18/200試行で異常終了）。
run_trial() {
    local pattern="$1" rps="$2" run_dir="$3"

    # SSH接続設定（リソースモニタリング用）
    _ssh_b=()
    if [ -n "${BASELINE_IP:-}" ]; then
        _ssh_b+=(--ssh-host "${BASELINE_IP}" --ssh-user "${BASELINE_SSH_USER:-root}" --ssh-port "${BASELINE_SSH_PORT:-22}")
    fi
    _ssh_p=()
    if [ -n "${PROPOSED_IP:-}" ]; then
        _ssh_p+=(--ssh-host "${PROPOSED_IP}" --ssh-user "${PROPOSED_SSH_USER:-root}" --ssh-port "${PROPOSED_SSH_PORT:-22}")
    fi

    # リソースモニター起動
    python3 "$PROJECT_ROOT/src/monitor/resource.py" \
        --compose-file "$PROJECT_ROOT/baseline/docker-compose.yml" \
        "${_ssh_b[@]}" \
        --output "$run_dir/resource_baseline.csv" \
        --duration $((DURATION + 30)) &
    MON_B=$!

    python3 "$PROJECT_ROOT/src/monitor/resource.py" \
        --compose-file "$PROJECT_ROOT/proposed/docker-compose.yml" \
        "${_ssh_p[@]}" \
        --output "$run_dir/resource_proposed.csv" \
        --duration $((DURATION + 30)) &
    MON_P=$!

    # 攻撃ジェネレーター実行
    python3 "$PROJECT_ROOT/src/generator/spike.py" \
        --targets "$BASELINE_URL" "$PROPOSED_URL" \
        --rps "$rps" \
        --duration "$DURATION" \
        --pattern "$pattern" \
        --output "$run_dir/inject_log.csv" || return 1

    # データ伝搬待機 — Redisキューが空になるまでポーリング（最大30分）
    MAX_WAIT=1800
    waited=0
    _queue_drained=false
    while [ $waited -lt $MAX_WAIT ]; do
        queue_len=$(queue_length "${PROPOSED_URL%/events}/health")
        if [ "$queue_len" = "0" ]; then
            echo "  キュー空 (${waited}s) → ClickHouse書き込みバッファ 5s"
            sleep 5
            _queue_drained=true
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

    # 欠損率計測
    python3 "$PROJECT_ROOT/src/measure/loss_rate.py" \
        --inject-log "$run_dir/inject_log.csv" \
        --clickhouse "$CLICKHOUSE_URL" \
        --elasticsearch "$ELASTICSEARCH_URL" \
        --proposed-target "$PROPOSED_URL" \
        --baseline-target "$BASELINE_URL" \
        --output "$run_dir/loss_rate.json" || return 1

    return 0
}

# ── 実験ループ ──
for pattern in "${PATTERNS[@]}"; do
  for rps in "${RPS_LEVELS[@]}"; do
    for trial in $(seq 1 "$TRIALS"); do
      current_run=$((current_run + 1))
      run_name="${pattern}_rps${rps}_trial${trial}"
      run_dir="$BATCH_DIR/$run_name"

      # ── レジューム判定: .done マーカーが存在すればスキップ（タイムアウトで終わった試行は再実行） ──
      DONE_MARKER="$run_dir/.done"
      if [ -f "$DONE_MARKER" ]; then
          skipped=$((skipped + 1))
          echo "[$current_run/$total_runs] $run_name — スキップ（完了済み）"
          continue
      fi

      mkdir -p "$run_dir"

      echo "──────────────────────────────────────"
      echo "[$current_run/$total_runs] $run_name"
      echo "──────────────────────────────────────"

      _queue_drained=false
      if run_trial "$pattern" "$rps" "$run_dir"; then
          trial_ok=true
      else
          trial_ok=false
          failed=$((failed + 1))
          echo "  [WARN] $run_name が異常終了しました（ネットワーク/接続エラーの可能性）。このトライアルはスキップして続行します（.doneが付かないため次回のレジューム実行で自動的に再試行対象になります）。"
      fi

      # リソースモニター停止（成功・失敗いずれの場合も後始末する）
      kill "$MON_B" "$MON_P" 2>/dev/null || true
      wait "$MON_B" "$MON_P" 2>/dev/null || true

      # 正常ドレイン完了時のみ完了マーカーを書く（タイムアウト・異常終了した試行は再実行対象とする）
      if [ "$trial_ok" = true ] && [ "$_queue_drained" = true ]; then
          touch "$DONE_MARKER"
      fi

      echo "[$current_run/$total_runs] $run_name 完了"

      # クールダウン
      if [ "$current_run" -lt "$total_runs" ]; then
          echo "  クールダウン ${COOLDOWN}s..."
          sleep "$COOLDOWN"
      fi
    done
  done
done

echo ""
echo "============================================"
echo " 全条件完了 (スキップ: ${skipped}/${total_runs}, 異常終了: ${failed}/${total_runs})"
echo " 結果を集約中..."
echo "============================================"

python3 "$PROJECT_ROOT/src/measure/aggregate.py" \
    --batch-dir "$BATCH_DIR" \
    --output "$BATCH_DIR/summary.json"

echo ""
echo "============================================"
echo " 実験完了"
echo " 完了: $((total_runs - skipped - failed))/${total_runs} 回（スキップ: ${skipped}、異常終了: ${failed}）"
echo " 結果: $BATCH_DIR/summary.json"
echo "============================================"

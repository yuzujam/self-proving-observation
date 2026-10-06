#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── run_ablation_batch.sh  — 集約ウィンドウ・アブレーション実験の複数条件・複数試行バッチ
#
#の統計的検証に
# 必要な複数のウィンドウ長（WINDOW_MS_LEVELS）×複数試行を積み上げる。
# run_multiedge_batch.sh・run_batch_resumable.sh と同じレジューム設計（.doneマーカー、
# BATCH_DIR再指定で再開）を、run_ablation_experiment.sh の単発実行に対して適用する。
#
# 使い方:
#   bash scripts/run_ablation_batch.sh                                # 新規実行
#   BATCH_DIR=results/ablation_batch_XXXXX bash scripts/run_ablation_batch.sh  # 中断再開

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"

# experiment.envが対照実験向けに`export DURATION=120`等を設定している場合、
# load_experiment_envのsourceが呼び出し元で指定したDURATIONを無条件で上書きしてしまう。
# run_ablation_experiment.sh・run_multiedge_experiment.sh
# 側は対策済みだが、この2つのbatchオーケストレーター自身も独立にload_experiment_envを
# 呼んでおり同じ問題を再発していた（実機で確認: DURATION未指定実行が意図の60sではなく
# 120sになった）。呼び出し元の値を退避し、load_experiment_env後に優先的に復元する。
_CALLER_DURATION="${DURATION:-}"
load_experiment_env
# 呼び出し元が明示指定しなかった場合、load_experiment_envが設定したexperiment.env
# 由来の値（対照実験用のDURATION=120等）を引き継がずunsetすることで、後段の
# `DURATION="${DURATION:-60}"`がこのスクリプト自身の既定値を正しく使えるようにする
# （2026-09-04追記: 「明示指定時に優先」だけでは
# 「未指定時にスクリプト既定値を使う」ことまでは保証されず、4件目の実害が発生した）。
if [ -n "$_CALLER_DURATION" ]; then
    DURATION="$_CALLER_DURATION"
else
    unset DURATION
fi

# ── 実験パラメータ（環境変数で上書き可能） ──
# 既定は現行の1000msを含む{100,500,1000,2000,5000}。
IFS=',' read -r -a WINDOW_MS_LEVELS <<< "${WINDOW_MS_LEVELS:-100,500,1000,2000,5000}"
TRIALS="${TRIALS:-5}"
BASE_RPS="${BASE_RPS:-100}"
DURATION="${DURATION:-60}"
PATTERN="${PATTERN:-flat}"
COOLDOWN="${COOLDOWN:-15}"

# ── 出力ディレクトリ（BATCH_DIR 指定でレジューム） ──
if [ -n "${BATCH_DIR:-}" ] && [ -d "$BATCH_DIR" ]; then
    echo "[RESUME] 既存バッチディレクトリを使用: $BATCH_DIR"
else
    BATCH_ID=$(date +%Y%m%d_%H%M%S)
    BATCH_DIR="$PROJECT_ROOT/results/ablation_batch_$BATCH_ID"
    mkdir -p "$BATCH_DIR"
    echo "[NEW] 新規バッチ: $BATCH_DIR"
fi

total_runs=$(( ${#WINDOW_MS_LEVELS[@]} * TRIALS ))
current_run=0
skipped=0
failed=0

echo "============================================"
echo " 集約ウィンドウ・アブレーション実験バッチ（レジューム対応）"
echo "============================================"
echo " WINDOW_MS_LEVELS: ${WINDOW_MS_LEVELS[*]}"
echo " 試行回数:         $TRIALS"
echo " BASE_RPS:         $BASE_RPS  PATTERN: $PATTERN  DURATION: ${DURATION}s"
echo " 合計:             $total_runs 回"
echo " 出力先:           $BATCH_DIR"
echo "============================================"
echo ""

_EXIT_CODE=0
_INTERRUPTED=false
cleanup() {
    if [ "$_INTERRUPTED" = true ] || [ "$_EXIT_CODE" -ne 0 ]; then
        local reason="終了コード: ${_EXIT_CODE}"
        [ "$_INTERRUPTED" = true ] && reason="Ctrl+C または SIGTERM により中断（再開: BATCH_DIR=${BATCH_DIR} bash scripts/run_ablation_batch.sh）"
        echo "[OBS FAILED] アブレーションバッチが失敗しました: ${BATCH_DIR##*/}（理由: ${reason}、進捗: ${current_run}/${total_runs} 回、スキップ: ${skipped}、結果: ${BATCH_DIR}）"
    fi
}
trap '_EXIT_CODE=$?; cleanup' EXIT
trap '_INTERRUPTED=true; exit 130' INT TERM

for window_ms in "${WINDOW_MS_LEVELS[@]}"; do
  for trial in $(seq 1 "$TRIALS"); do
    current_run=$((current_run + 1))
    run_name="w${window_ms}_trial${trial}"
    run_dir="$BATCH_DIR/$run_name"

    DONE_MARKER="$run_dir/.done"
    if [ -f "$DONE_MARKER" ]; then
        skipped=$((skipped + 1))
        echo "[$current_run/$total_runs] $run_name — スキップ（完了済み）"
        continue
    fi

    echo "──────────────────────────────────────"
    echo "[$current_run/$total_runs] $run_name"
    echo "──────────────────────────────────────"

    if WINDOW_MS="$window_ms" \
       BASE_RPS="$BASE_RPS" \
       DURATION="$DURATION" \
       PATTERN="$PATTERN" \
       ABLATION_RESULTS_DIR="$run_dir" \
       bash "$SCRIPT_DIR/run_ablation_experiment.sh"; then
        if [ -f "$run_dir/results.json" ]; then
            touch "$DONE_MARKER"
        else
            failed=$((failed + 1))
            echo "  [WARN] $run_name は正常終了したが results.json が見つかりません。次回のレジューム実行で再試行対象になります。"
        fi
    else
        failed=$((failed + 1))
        echo "  [WARN] $run_name が異常終了しました。次回のレジューム実行で再試行対象になります。"
    fi

    echo "[$current_run/$total_runs] $run_name 完了"

    if [ "$current_run" -lt "$total_runs" ]; then
        echo "  クールダウン ${COOLDOWN}s..."
        sleep "$COOLDOWN"
    fi
  done
done

echo ""
echo "============================================"
echo " 全条件完了（スキップ: ${skipped}/${total_runs}, 異常終了: ${failed}/${total_runs}）"
echo " 結果: $BATCH_DIR/*/results.json"
echo "============================================"

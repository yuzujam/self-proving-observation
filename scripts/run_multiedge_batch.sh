#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── run_multiedge_batch.sh  — マルチエッジ・スケーラビリティ検証の複数条件・複数試行バッチ
#
# doc/pipeline-spec.md「補強実験: 合成マルチエッジ・スケーラビリティ検証」の
# 統計的検証（Cochran-Armitage傾向検定）に必要な複数条件（EDGE_COUNT）×複数試行を
# 積み上げる。run_batch_resumable.sh と同じレジューム設計（.done マーカー、
# BATCH_DIR再指定で再開）を、run_multiedge_experiment.sh の単発実行に対して適用する。
#
# 使い方:
#   bash scripts/run_multiedge_batch.sh                              # 新規実行（localhost）
#   BATCH_DIR=results/multiedge_batch_XXXXX bash scripts/run_multiedge_batch.sh  # 中断再開
#
#   本番proposed-nodeへ向ける場合は run_multiedge_experiment.sh と同じガードレールが
#   そのまま効く（PROPOSED_URL に本番IPを指定する場合は OBS_MULTIEDGE_CONFIRM_PROD=1
#   が必須、内部指針 5.2）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"

# experiment.envが対照実験向けに`export DURATION=120`等を設定している場合、
# load_experiment_envのsourceが呼び出し元で指定したDURATIONを無条件で上書きしてしまう
# （`known-limitations.md` #HH）。run_multiedge_experiment.sh側は対策済みだが、この
# batchオーケストレーター自身も独立にload_experiment_envを呼んでおり同じ問題を
# 再発しうる。呼び出し元の値を退避し、load_experiment_env後に優先的に復元する。
_CALLER_DURATION="${DURATION:-}"
load_experiment_env
# 呼び出し元が明示指定しなかった場合、load_experiment_envが設定したexperiment.env
# 由来の値（対照実験用のDURATION=120等）を引き継がずunsetすることで、後段の
# `DURATION="${DURATION:-60}"`がこのスクリプト自身の既定値を正しく使えるようにする
# （`known-limitations.md` #HH 2026-09-04追記: 「明示指定時に優先」だけでは
# 「未指定時にスクリプト既定値を使う」ことまでは保証されず、4件目の実害が発生した）。
if [ -n "$_CALLER_DURATION" ]; then
    DURATION="$_CALLER_DURATION"
else
    unset DURATION
fi

# ── 実験パラメータ（環境変数で上書き可能） ──
# 既定はdoc/pipeline-spec.mdが候補とするN∈{1,2,4,8}。
IFS=',' read -r -a EDGE_COUNTS <<< "${EDGE_COUNTS:-1,2,4,8}"
IFS=',' read -r -a EDGE_MODES <<< "${EDGE_MODES:-fixed-total,scaled}"
TRIALS="${TRIALS:-5}"
BASE_RPS="${BASE_RPS:-100}"
DURATION="${DURATION:-60}"
PATTERN="${PATTERN:-flat}"
COOLDOWN="${COOLDOWN:-30}"

PROPOSED_URL="${PROPOSED_URL:-http://localhost:8000/events}"
CLICKHOUSE_URL="${CLICKHOUSE_URL:-http://localhost:8123}"

# ── 出力ディレクトリ（BATCH_DIR 指定でレジューム） ──
if [ -n "${BATCH_DIR:-}" ] && [ -d "$BATCH_DIR" ]; then
    echo "[RESUME] 既存バッチディレクトリを使用: $BATCH_DIR"
else
    BATCH_ID=$(date +%Y%m%d_%H%M%S)
    BATCH_DIR="$PROJECT_ROOT/results/multiedge_batch_$BATCH_ID"
    mkdir -p "$BATCH_DIR"
    echo "[NEW] 新規バッチ: $BATCH_DIR"
fi

total_runs=$(( ${#EDGE_COUNTS[@]} * ${#EDGE_MODES[@]} * TRIALS ))
current_run=0
skipped=0
failed=0

echo "============================================"
echo " マルチエッジ・スケーラビリティ検証バッチ（レジューム対応）"
echo "============================================"
echo " EDGE_COUNTS: ${EDGE_COUNTS[*]}"
echo " EDGE_MODES:  ${EDGE_MODES[*]}"
echo " 試行回数:    $TRIALS"
echo " BASE_RPS:    $BASE_RPS  PATTERN: $PATTERN  DURATION: ${DURATION}s"
echo " 合計:        $total_runs 回"
echo " 出力先:      $BATCH_DIR"
echo " 送信先:      $PROPOSED_URL"
echo "============================================"
echo ""

_EXIT_CODE=0
_INTERRUPTED=false
cleanup() {
    if [ "$_INTERRUPTED" = true ] || [ "$_EXIT_CODE" -ne 0 ]; then
        local reason="終了コード: ${_EXIT_CODE}"
        [ "$_INTERRUPTED" = true ] && reason="Ctrl+C または SIGTERM により中断（再開: BATCH_DIR=${BATCH_DIR} bash scripts/run_multiedge_batch.sh）"
        echo "[OBS FAILED] マルチエッジバッチが失敗しました: ${BATCH_DIR##*/}（理由: ${reason}、進捗: ${current_run}/${total_runs} 回、スキップ: ${skipped}、結果: ${BATCH_DIR}）"
    fi
}
trap '_EXIT_CODE=$?; cleanup' EXIT
trap '_INTERRUPTED=true; exit 130' INT TERM

for edge_count in "${EDGE_COUNTS[@]}"; do
  for edge_mode in "${EDGE_MODES[@]}"; do
    for trial in $(seq 1 "$TRIALS"); do
      current_run=$((current_run + 1))
      run_name="N${edge_count}_${edge_mode}_trial${trial}"
      run_dir="$BATCH_DIR/$run_name"

      # ── レジューム判定: .done マーカーが存在すればスキップ ──
      DONE_MARKER="$run_dir/.done"
      if [ -f "$DONE_MARKER" ]; then
          skipped=$((skipped + 1))
          echo "[$current_run/$total_runs] $run_name — スキップ（完了済み）"
          continue
      fi

      echo "──────────────────────────────────────"
      echo "[$current_run/$total_runs] $run_name"
      echo "──────────────────────────────────────"

      if EDGE_COUNT="$edge_count" \
         EDGE_MODE="$edge_mode" \
         BASE_RPS="$BASE_RPS" \
         DURATION="$DURATION" \
         PATTERN="$PATTERN" \
         PROPOSED_URL="$PROPOSED_URL" \
         CLICKHOUSE_URL="$CLICKHOUSE_URL" \
         MULTIEDGE_RESULTS_DIR="$run_dir" \
         bash "$SCRIPT_DIR/run_multiedge_experiment.sh"; then
          # loss_rate.json・meta.json の両方が揃っていることを完了の条件とする
          # （analyze_multiedge_results.py の load_trial と同じ判定基準）。
          if [ -f "$run_dir/loss_rate.json" ] && [ -f "$run_dir/meta.json" ]; then
              touch "$DONE_MARKER"
          else
              failed=$((failed + 1))
              echo "  [WARN] $run_name は正常終了したが出力ファイルが不完全です。次回のレジューム実行で再試行対象になります。"
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
done

echo ""
echo "============================================"
echo " 全条件完了（スキップ: ${skipped}/${total_runs}, 異常終了: ${failed}/${total_runs}）"
echo " 結果を集約中..."
echo "============================================"

python3 "$PROJECT_ROOT/scripts/analyze_multiedge_results.py" \
    "$BATCH_DIR"/*/ \
    --output "$BATCH_DIR/analysis.json"

echo ""
echo "============================================"
echo " バッチ完了"
echo " 完了: $((total_runs - skipped - failed))/${total_runs} 回（スキップ: ${skipped}、異常終了: ${failed}）"
echo " 結果: $BATCH_DIR/analysis.json"
echo "============================================"

#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── run_full_thesis.sh  — 修士論文用 完全自動実験パイプライン
#
# 使い方:
#   bash scripts/run_full_thesis.sh              # 全工程実行
#   bash scripts/run_full_thesis.sh --resume     # 中断からの再開
#   bash scripts/run_full_thesis.sh --skip-infra # インフラ起動をスキップ
#
# 全工程:
#   1. 環境起動（Proposed + Baseline 接続確認）
#   2. 対照実験バッチ（4パターン × 5RPS × TRIALS試行。TRIALSの既定は5=100回、
#      本番のexperiment.envでは10=200回）
#   3. 統計検定（Mann-Whitney U, 効果量, 信頼区間）
#   4. 論文用可視化（箱ひげ図, ヒートマップ, スループット曲線）
#   5. Fidelity Guard 実験（3シナリオ × 10試行 = 30回、シードは起動時刻から算出）
#   6. 最終レポート集約

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"

# ── 引数パース ──
RESUME=false
SKIP_INFRA=false
for arg in "$@"; do
    case "$arg" in
        --resume) RESUME=true ;;
        --skip-infra) SKIP_INFRA=true ;;
    esac
done

# ── 設定ファイル読み込み ──
load_experiment_env

# ── 多重起動防止ロック ──
# run_cron.sh 側の /tmp/obs_experiment.lock は run_cron.sh 自身の二重起動しか
# 防げず、本スクリプトを直接手動実行（例: --resume）した場合はすり抜ける
# （2026-07-08、cron起動分と手動--resume起動分が同一batch_dirに対して並行実行し、
# 同一trialのデータを競合上書きする事故が発生。doc/known-limitations.md参照）。
# 起動経路によらず本スクリプト自体を多重起動禁止にする。
FULL_THESIS_LOCK="/tmp/obs_full_thesis.lock"
exec 200>"$FULL_THESIS_LOCK"
if ! flock -n 200; then
    echo "[FATAL] 別の run_full_thesis.sh が既に実行中です。多重起動防止ロックにより中止します。" >&2
    exit 1
fi

BASELINE_IP="${BASELINE_IP:-}"
PROPOSED_URL="${PROPOSED_URL:-http://localhost:8000/events}"
CLICKHOUSE_URL="${CLICKHOUSE_URL:-http://localhost:8123}"
ELASTICSEARCH_URL="${ELASTICSEARCH_URL:-http://${BASELINE_IP}:9200}"

THESIS_ID=$(date +%Y%m%d_%H%M%S)
THESIS_DIR="$PROJECT_ROOT/results/thesis_${THESIS_ID}"
LOG_FILE="$THESIS_DIR/thesis_experiment.log"
mkdir -p "$THESIS_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

_EXIT_CODE=0
_INTERRUPTED=false
cleanup() {
    local code=${_EXIT_CODE}
    log "[CLEANUP] 完了"

    if [ "$_INTERRUPTED" = true ] || [ "$code" -ne 0 ]; then
        local reason="終了コード: $code"
        [ "$_INTERRUPTED" = true ] && reason="Ctrl+C または SIGTERM により中断（再開: bash scripts/run_full_thesis.sh --resume）"
        log "[OBS FAILED] 実験が失敗しました: ${THESIS_ID}（理由: ${reason}）"
    fi
}
trap '_EXIT_CODE=$?; cleanup' EXIT
trap '_INTERRUPTED=true; exit 130' INT TERM

log "============================================"
log " 論文用 完全自動実験"
log " $THESIS_ID"
log " Resume: $RESUME"
log "============================================"

# ─────────────────────────────────────
# Step 0: プリフライトチェック
# ─────────────────────────────────────
log "[0/6] プリフライトチェック..."
PREFLIGHT_OK=true

for cmd in python3 curl; do
    if ! command -v "$cmd" > /dev/null 2>&1; then
        log "  [NG] $cmd が見つかりません"
        PREFLIGHT_OK=false
    else
        log "  [OK] $cmd"
    fi
done

for mod in numpy scipy torch shap matplotlib; do
    if python3 -c "import $mod" 2>/dev/null; then
        log "  [OK] Python: $mod"
    else
        log "  [NG] Python: $mod がインストールされていません"
        log "       → pip install -r requirements.txt"
        PREFLIGHT_OK=false
    fi
done


if [ "$PREFLIGHT_OK" = false ]; then
    log "[FATAL] プリフライトチェック失敗。上記を修正してください。"
    exit 1
fi
log "  全チェック OK"
echo ""

START_TOTAL=$(date +%s)

# ─────────────────────────────────────
# Step 1: 環境起動
# ─────────────────────────────────────
if [ "$SKIP_INFRA" = false ]; then
    log "[1/6] リモートサーバー接続確認..."
    health_ok "${PROPOSED_URL%/events}/health" \
        || { log "[FATAL] Proposed (${PROPOSED_URL}) に接続できません"; exit 1; }
    log "  Proposed: OK"

    health_ok "$CLICKHOUSE_URL" \
        || { log "[FATAL] ClickHouse ($CLICKHOUSE_URL) に接続できません"; exit 1; }
    log "  ClickHouse: OK"

    health_ok "$ELASTICSEARCH_URL/_cluster/health" \
        || { log "[FATAL] Baseline ($ELASTICSEARCH_URL) に接続できません"; exit 1; }
    log "  Baseline: OK"
else
    log "[1/6] 接続確認スキップ (--skip-infra)"
fi

# ─────────────────────────────────────
# Step 2: 対照実験バッチ
# ─────────────────────────────────────
log "[2/6] 対照実験バッチ開始..."
BATCH_START=$(date +%s)

if [ "$RESUME" = true ]; then
    LATEST_BATCH=$(ls -td "$PROJECT_ROOT/results/batch_"* 2>/dev/null | head -1)
    if [ -n "$LATEST_BATCH" ] && [ -d "$LATEST_BATCH" ]; then
        log "  既存バッチを検出: $LATEST_BATCH"
        export BATCH_DIR="$LATEST_BATCH"
        log "  未完了の試行を再実行します..."
        bash "$SCRIPT_DIR/run_batch_resumable.sh" 2>&1 | tee -a "$LOG_FILE"
    else
        log "  再開可能なバッチが見つかりません。新規実行します。"
        bash "$SCRIPT_DIR/run_batch_resumable.sh" 2>&1 | tee -a "$LOG_FILE"
    fi
else
    bash "$SCRIPT_DIR/run_batch_resumable.sh" 2>&1 | tee -a "$LOG_FILE"
fi

BATCH_END=$(date +%s)
BATCH_ELAPSED=$(( BATCH_END - BATCH_START ))
log "  対照実験所要時間: $(( BATCH_ELAPSED / 3600 ))h $(( (BATCH_ELAPSED % 3600) / 60 ))m"

BATCH_DIR="${BATCH_DIR:-$(ls -td "$PROJECT_ROOT/results/batch_"* 2>/dev/null | head -1)}"

if [ -z "$BATCH_DIR" ] || [ ! -d "$BATCH_DIR" ]; then
    log "[FATAL] バッチ結果ディレクトリが見つかりません"
    exit 1
fi
log "  バッチ結果: $BATCH_DIR"

# ─────────────────────────────────────
# Step 3: 統計検定
# ─────────────────────────────────────
log "[3/6] 統計検定..."
STATS_DIR="$THESIS_DIR/statistics"
mkdir -p "$STATS_DIR"

(cd "$PROJECT_ROOT" && python3 -m src.measure.stats \
    --batch-dir "$BATCH_DIR" \
    --output-dir "$STATS_DIR") \
    2>&1 | tee -a "$LOG_FILE"

log "  統計結果: $STATS_DIR"

# ─────────────────────────────────────
# Step 4: 論文用可視化
# ─────────────────────────────────────
log "[4/6] 論文用可視化..."
FIGURES_DIR="$THESIS_DIR/figures"
mkdir -p "$FIGURES_DIR"

(cd "$PROJECT_ROOT" && python3 -m src.visualize.batch_report \
    --batch-dir "$BATCH_DIR" \
    --output-dir "$FIGURES_DIR") \
    2>&1 | tee -a "$LOG_FILE"

# 個別実験のリソース比較プロットも生成
for run_dir in "$BATCH_DIR"/*/; do
    [ -d "$run_dir" ] || continue
    baseline_csv="$run_dir/resource_baseline.csv"
    proposed_csv="$run_dir/resource_proposed.csv"

    if [ -f "$baseline_csv" ] && [ -f "$proposed_csv" ]; then
        (cd "$PROJECT_ROOT" && python3 -c "
from src.visualize.report import plot_resource_comparison
plot_resource_comparison('$baseline_csv', '$proposed_csv', '${run_dir}resource_comparison.png')
") 2>/dev/null || true
    fi
done

log "  図表: $FIGURES_DIR"

# ─────────────────────────────────────
# Step 5: Fidelity Guard 実験
# ─────────────────────────────────────
log "[5/6] Fidelity Guard 実験..."
FIDELITY_DIR="$THESIS_DIR/fidelity"
mkdir -p "$FIDELITY_DIR"

FIDELITY_START=$(date +%s)

# THESIS_FIDELITY_SEED未指定時は起動時刻（エポック秒）からseedを算出する。
# 本呼び出しは従来--seedを渡しておらずrun_fidelity_experiment.pyの既定値
# seed=42が毎回使われていたため、run_cron.sh経由で本スクリプトが繰り返し
# 実行される20回分すべてでthesis_*/fidelity/が同一データの複製になっていた
# （known-limitations.md #Vと同型だが#Vの修正対象外だった別経路、2026-08-03発見）。
# 週次のrun_fidelity_cron.sh（FIDELITY_SEED_BASE=42から+10刻み）とseed空間が
# 衝突しないよう、エポック秒ベースの値を用いる。
THESIS_FIDELITY_SEED="${THESIS_FIDELITY_SEED:-$FIDELITY_START}"
log "  Fidelity Guard seed: $THESIS_FIDELITY_SEED"

python3 "$SCRIPT_DIR/run_fidelity_experiment.py" \
    --output-dir "$FIDELITY_DIR" \
    --trials 10 \
    --epochs 50 \
    --scenario all \
    --seed "$THESIS_FIDELITY_SEED" \
    2>&1 | tee -a "$LOG_FILE"

FIDELITY_END=$(date +%s)
FIDELITY_ELAPSED=$(( FIDELITY_END - FIDELITY_START ))
log "  Fidelity Guard 所要時間: $(( FIDELITY_ELAPSED / 60 ))m $(( FIDELITY_ELAPSED % 60 ))s"
log "  結果: $FIDELITY_DIR"

# Fidelity Guard の LaTeX 表を生成
# confirming_detected/detection_lag（確認指標、doc/decisions.md）を用いた表を
# 論文投稿用として生成する。旧指標 fidelity_leads ベースの表（export_latex_fidelity_table）
# は後方互換のため関数自体は残しているが、本スクリプトからは呼び出さない。
if [ -f "$FIDELITY_DIR/fidelity_summary.json" ]; then
    (cd "$PROJECT_ROOT" && python3 -c "
import json
from src.measure.stats import export_latex_confirming_detection_table
with open('$FIDELITY_DIR/fidelity_summary.json') as f:
    summary = json.load(f)
export_latex_confirming_detection_table(summary, '$STATS_DIR/table_fidelity.tex')
") 2>&1 | tee -a "$LOG_FILE"
fi

# ─────────────────────────────────────
# Step 6: 最終サマリー
# ─────────────────────────────────────
END_TOTAL=$(date +%s)
TOTAL_ELAPSED=$(( END_TOTAL - START_TOTAL ))
TOTAL_HOURS=$(( TOTAL_ELAPSED / 3600 ))
TOTAL_MINS=$(( (TOTAL_ELAPSED % 3600) / 60 ))

log ""
log "============================================"
log " 全実験完了"
log "============================================"
log " 総所要時間:     ${TOTAL_HOURS}h ${TOTAL_MINS}m"
log " 結果ディレクトリ: $THESIS_DIR"
log ""
log " 📂 出力構成:"
log "   $THESIS_DIR/"
log "   ├── statistics/"
log "   │   ├── summary_stats.json    — 統計検定結果"
log "   │   ├── table_loss_rate.tex   — 欠損率 LaTeX 表"
log "   │   ├── table_cpu_peak.tex    — CPU LaTeX 表"
log "   │   └── table_fidelity.tex   — FG 確認指標（confirming_detected）LaTeX 表"
log "   ├── figures/"
log "   │   ├── boxplot_loss_rate.png — 欠損率箱ひげ図"
log "   │   ├── boxplot_resources.png — リソース箱ひげ図"
log "   │   ├── heatmap_loss_*.png    — 欠損率ヒートマップ"
log "   │   └── throughput_curve.png  — スループット曲線"
log "   ├── fidelity/"
log "   │   ├── sudden_trial*/        — 突発ドリフト実験"
log "   │   ├── gradual_trial*/       — 緩やかドリフト実験"
log "   │   ├── recurring_trial*/     — 周期ドリフト実験"
log "   │   └── fidelity_summary.json — FG 実験サマリー"
log "   └── thesis_experiment.log     — 実験ログ"
log ""
log " 論文への挿入:"
log "   \\input{$STATS_DIR/table_loss_rate.tex}"
log "   \\input{$STATS_DIR/table_cpu_peak.tex}"
log "============================================"

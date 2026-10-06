#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── run_ablation_experiment.sh  — 集約ウィンドウのアブレーション実験（単発実行）
#
# proposed/docker-compose.ablation.yml が構築するVectorコンテナ1つのみの隔離スタックへ、
# src/generator/spike.py --file-output でSuricata eve.json互換のNDJSONを直接投入し、
# 集約ウィンドウ長（WINDOW_MS）ごとの正確性（イベント取りこぼしの有無）とレイテンシを
# 計測する。本番の90日間観測パイプライン（proposed/docker-compose.yml）とは
# ネットワーク・ボリューム・コンテナ名とも非接続のため、本番観測への影響はない設計。
#
# 使い方:
#   WINDOW_MS=1000 BASE_RPS=100 DURATION=60 bash scripts/run_ablation_experiment.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"

# experiment.envが対照実験向けに`export DURATION=120`等を設定している場合、
# load_experiment_envのsourceが呼び出し元で指定したDURATIONを無条件で上書きしてしまう。
# 呼び出し元の値を退避し、
# load_experiment_env後に優先的に復元することで防ぐ。
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

# ── パラメータ（環境変数で上書き可能） ──
WINDOW_MS="${WINDOW_MS:-1000}"
BASE_RPS="${BASE_RPS:-100}"
DURATION="${DURATION:-60}"
PATTERN="${PATTERN:-flat}"
# 既定の送信元IP生成（ほぼユニーク）だとgroup_by=[sensor_id,event_type]が
# ほとんど1グループ=1イベントになり、reduceが実際に複数イベントを集約する
# 場面を検証できない（2026-09-03の実機テストで発覚）。
# 小さなプールに限定し、同一キーの繰り返し出現を作る。
SENSOR_POOL_SIZE="${SENSOR_POOL_SIZE:-5}"
COMPOSE_FILE="$PROJECT_ROOT/proposed/docker-compose.ablation.yml"
# proposed/ディレクトリには本番docker-compose.ymlも同居しており、docker composeは
# 未指定時にディレクトリ名（proposed）をプロジェクト名として使うため、明示しないと
# 本番スタックと同じプロジェクト名を共有し「orphan containers」として誤検知される
# （実機確認済み）。専用プロジェクト名で完全に名前空間を分離する。
COMPOSE_PROJECT="obs-ablation"

if ! [[ "$WINDOW_MS" =~ ^[0-9]+$ ]] || [ "$WINDOW_MS" -lt 1 ]; then
    echo "[ERROR] WINDOW_MS は1以上の整数である必要があります（現在値: $WINDOW_MS）" >&2
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
# ABLATION_RESULTS_DIR: run_ablation_batch.sh がレジューム判定用に固定パスを
# 指定するためのオプショナル上書き（未指定時は従来通りタイムスタンプ自動生成）。
RESULTS_DIR="${ABLATION_RESULTS_DIR:-$PROJECT_ROOT/results/ablation_${TIMESTAMP}_w${WINDOW_MS}}"
mkdir -p "$RESULTS_DIR"
# docker-compose.ablation.ymlの必須変数ABLATION_DATA_DIRをexportしておく。
# 個別のdocker composeコマンド呼び出しに前置するだけでは、この後バックグラウンド
# ジョブとして起動するresource.py（内部でも`docker compose ps -q`を呼ぶ）に
# 環境変数が伝播せず、コンテナ検出が常に失敗してリソース計測がホスト全体の
# 値へサイレントにフォールバックし続けていた。
export ABLATION_DATA_DIR="$RESULTS_DIR"

EVE_LOG="$RESULTS_DIR/eve.json"
AGG_OUTPUT="$RESULTS_DIR/aggregated_output.jsonl"
touch "$EVE_LOG"

# Vector自身の実行時env var展開は非文字列コンテキスト（裸の数値）では機能しない
# ことを実機で確認したため、WINDOW_MSはsedでテンプレートに直接埋め込んだ実体の
# TOMLを$RESULTS_DIR（コンテナ内/dataとしてマウント）へ生成する。
sed "s/__WINDOW_MS__/$WINDOW_MS/" "$PROJECT_ROOT/proposed/vector-ablation.toml" \
    > "$RESULTS_DIR/vector-ablation.generated.toml"

# グレース期間: 集約ウィンドウの最大待機時間（WINDOW_MS）+ Vectorのフラッシュ・
# ファイルI/Oバッファ分の余裕。これを過ぎても未フラッシュのレコードは
# 正確性チェックで生成件数との差分として現れる。
GRACE_S=$(( (WINDOW_MS + 999) / 1000 + 10 ))
MEASURE_DURATION=$(( DURATION + GRACE_S ))

echo "============================================"
echo " 集約ウィンドウ・アブレーション実験"
echo " $TIMESTAMP"
echo "============================================"
echo " WINDOW_MS=$WINDOW_MS  BASE_RPS=$BASE_RPS  DURATION=${DURATION}s  PATTERN=$PATTERN"
echo " 結果ディレクトリ: $RESULTS_DIR"
echo "============================================"
echo ""

MEASURE_PID=""
MON_PID=""
_EXIT_CODE=0
_INTERRUPTED=false
cleanup() {
    kill "${MEASURE_PID:-}" "${MON_PID:-}" 2>/dev/null || true
    wait "${MEASURE_PID:-}" "${MON_PID:-}" 2>/dev/null || true
    # コンテナ停止前にログを保存する（停止後は`docker logs`で参照できなくなるため、
    # 集約0件等の異常時にVector側の警告・エラーを追える唯一の手がかりになる）。
    ABLATION_DATA_DIR="$RESULTS_DIR" docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" logs vector-ablation \
        > "$RESULTS_DIR/vector.log" 2>&1 || true
    ABLATION_DATA_DIR="$RESULTS_DIR" docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" down 2>/dev/null || true
    if [ "$_INTERRUPTED" = true ] || [ "$_EXIT_CODE" -ne 0 ]; then
        local reason="終了コード: $_EXIT_CODE"
        [ "$_INTERRUPTED" = true ] && reason="Ctrl+C または SIGTERM により中断"
        echo "[OBS FAILED] アブレーション実験が失敗しました: ${TIMESTAMP}（理由: ${reason}、WINDOW_MS: ${WINDOW_MS}、結果ディレクトリ: ${RESULTS_DIR}）"
    fi
}
trap '_EXIT_CODE=$?; cleanup' EXIT
trap '_INTERRUPTED=true; exit 130' INT TERM

# --- Step 1: 隔離Vectorスタック起動 ---
echo "[1/5] 隔離Vectorコンテナ起動 (expire_after_ms=$WINDOW_MS)..."
# 前回実行がCtrl+C等で中断されコンテナが`Created`/`Exited`のまま残っている場合、
# container_name固定のため次回の`up`が名前衝突で失敗する。起動前に必ず一度
# down（idempotent、対象がなければ何もしない）しておくことで自己修復させる。
ABLATION_DATA_DIR="$RESULTS_DIR" docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" down 2>/dev/null || true
ABLATION_DATA_DIR="$RESULTS_DIR" docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" up -d
sleep 3
echo ""

# --- Step 2: レイテンシ計測・リソースモニター開始（バックグラウンド） ---
echo "[2/5] レイテンシ計測開始 (${MEASURE_DURATION}s)..."
python3 "$PROJECT_ROOT/scripts/measure_ablation_latency.py" \
    --output-file "$AGG_OUTPUT" \
    --duration "$MEASURE_DURATION" \
    --result-output "$RESULTS_DIR/latency_result.json" &
MEASURE_PID=$!
echo "  PID: $MEASURE_PID"

python3 "$PROJECT_ROOT/src/monitor/resource.py" \
    --compose-file "$COMPOSE_FILE" \
    --project-name "$COMPOSE_PROJECT" \
    --output "$RESULTS_DIR/resource_vector.csv" \
    --duration "$MEASURE_DURATION" &
MON_PID=$!
echo "  リソースモニターPID: $MON_PID"
echo ""

# --- Step 3: イベント生成（Vectorのfileソースへ直接投入） ---
echo "[3/5] イベント生成 (rps=$BASE_RPS pattern=$PATTERN)..."
python3 "$PROJECT_ROOT/src/generator/spike.py" \
    --file-output "$EVE_LOG" \
    --rps "$BASE_RPS" \
    --duration "$DURATION" \
    --pattern "$PATTERN" \
    --sensor-pool-size "$SENSOR_POOL_SIZE" \
    --output "$RESULTS_DIR/index.csv"
echo ""

# --- Step 4: 計測完了待機 ---
echo "[4/5] グレース期間待機・計測完了待ち..."
wait "$MEASURE_PID"
MEASURE_PID=""
kill "$MON_PID" 2>/dev/null || true
wait "$MON_PID" 2>/dev/null || true
MON_PID=""
echo ""

# --- Step 5: 正確性チェック & メタデータ保存（コンテナ停止はcleanupで実施） ---
echo "[5/5] 正確性チェック & メタデータ保存..."
RAW_EVENT_COUNT=$(( $(wc -l < "$RESULTS_DIR/index.csv") - 1 ))
python3 - "$RESULTS_DIR/latency_result.json" "$RESULTS_DIR/results.json" "$RAW_EVENT_COUNT" "$WINDOW_MS" "$BASE_RPS" "$DURATION" "$PATTERN" "$TIMESTAMP" <<'PYEOF'
import json
import sys

latency_path, out_path, raw_count, window_ms, base_rps, duration, pattern, timestamp = sys.argv[1:9]
with open(latency_path, encoding="utf-8") as f:
    latency = json.load(f)

raw_count = int(raw_count)
total_count = latency["total_count"]
result = {
    "timestamp": timestamp,
    "window_ms": int(window_ms),
    "base_rps": int(base_rps),
    "duration": int(duration),
    "pattern": pattern,
    "raw_event_count": raw_count,
    "aggregated_count_sum": total_count,
    "count_matches": raw_count == total_count,
    "count_diff": total_count - raw_count,
    "n_aggregated_groups": latency["n_groups"],
    "parse_errors": latency["parse_errors"],
    "latency_upper_ms": latency["latency_upper_ms"],
    "latency_lower_ms": latency["latency_lower_ms"],
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print(f"  raw_event_count={raw_count}  aggregated_count_sum={total_count}  "
      f"count_matches={result['count_matches']}")
print(f"  latency_upper_ms(mean)={latency['latency_upper_ms']['mean']}  "
      f"latency_lower_ms(mean)={latency['latency_lower_ms']['mean']}")
PYEOF

echo ""
echo "============================================"
echo " 実験完了: $RESULTS_DIR/results.json"
echo "============================================"

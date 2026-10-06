#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── run_multiedge_experiment.sh  — 合成マルチエッジ・スケーラビリティ検証（単発実行）
#
# doc/pipeline-spec.md「補強実験: 合成マルチエッジ・スケーラビリティ検証」の実装。
# 90日間の実地観測・対照実験がエッジ1台:中央1台の構成のみで、複数エッジノードが
# 同時に稼働した場合の中央受付層（FastAPI+Redis+ClickHouse）のスケーラビリティを
# 未検証のままにしている問題（doc/known-limitations.md #8）への対処。
#
# 既存コンポーネント（src/generator/spike.py・src/measure/loss_rate.py）は無変更で
# 再利用する。「仮想エッジ」は同一ホスト上で並行実行する複数の spike.py プロセスで
# 表現し、いずれも中央ノードの同一受付エンドポイント（PROPOSED_URL）へ送信する。
#
# 本スクリプトは1回の (EDGE_COUNT, EDGE_MODE) 条件を単発実行するのみ。
# 複数条件・複数試行のバッチ化（run_batch_resumable.sh 相当の中断・再開対応）は
# 未実装（doc/pipeline-spec.md 参照、今後の課題）。
#
# 実行前に必ず確認すること（内部指針 5.2）:
#   本番proposed-nodeのエンドポイントへ向けて実行すると、90日間観測本体と
#   同じ受付・書き込みパイプラインへ合成負荷をかけることになる。本番へ向ける
#   場合は、観測への影響（一時的な負荷増）を説明した上で個別に確認を得ること。
#   デフォルトはlocalhostを対象とし、本番IPを対象にする場合は
#   PROPOSED_URL を明示的に上書きすること。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"

# experiment.envが対照実験向けに`export DURATION=120`等を設定している場合、
# load_experiment_envのsourceが呼び出し元で指定したDURATIONを無条件で上書きしてしまう
# （`known-limitations.md` #HH）。呼び出し元の値を退避し、load_experiment_env後に
# 優先的に復元することで防ぐ（既存の単発対照実験系スクリプトの挙動は無変更）。
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

# ── パラメータ（環境変数で上書き可能） ──
EDGE_COUNT="${EDGE_COUNT:-1}"
# fixed-total: 総送信レートを EDGE_COUNT で分割（1エッジの負荷をN分割、劣化しないことが期待値）
# scaled     : 各エッジが BASE_RPS を送信（総負荷が EDGE_COUNT 倍に線形増加、中央側の限界点を探る）
EDGE_MODE="${EDGE_MODE:-fixed-total}"
BASE_RPS="${BASE_RPS:-100}"
DURATION="${DURATION:-60}"
PATTERN="${PATTERN:-flat}"
PROPOSED_URL="${PROPOSED_URL:-http://localhost:8000/events}"
CLICKHOUSE_URL="${CLICKHOUSE_URL:-http://localhost:8123}"

if ! [[ "$EDGE_COUNT" =~ ^[0-9]+$ ]] || [ "$EDGE_COUNT" -lt 1 ]; then
    echo "[ERROR] EDGE_COUNT は1以上の整数である必要があります（現在値: $EDGE_COUNT）" >&2
    exit 1
fi
if [ "$EDGE_MODE" != "fixed-total" ] && [ "$EDGE_MODE" != "scaled" ]; then
    echo "[ERROR] EDGE_MODE は fixed-total または scaled のいずれかである必要があります（現在値: $EDGE_MODE）" >&2
    exit 1
fi

# 本番IPへ向ける場合は事前確認を促す（誤って無確認で本番へ負荷をかけることを防ぐ）
case "$PROPOSED_URL" in
    *203.0.113.10*)
        if [ "${OBS_MULTIEDGE_CONFIRM_PROD:-0}" != "1" ]; then
            echo "[ERROR] PROPOSED_URL が本番proposed-nodeを指しています。" >&2
            echo "        90日間観測本体と同じ受付パイプラインへ合成負荷をかけることになります。" >&2
            echo "        影響を確認の上で実行する場合は OBS_MULTIEDGE_CONFIRM_PROD=1 を設定してください。" >&2
            exit 1
        fi
        ;;
esac

if [ "$EDGE_MODE" = "fixed-total" ]; then
    PER_EDGE_RPS=$(( BASE_RPS / EDGE_COUNT ))
    [ "$PER_EDGE_RPS" -lt 1 ] && PER_EDGE_RPS=1
else
    PER_EDGE_RPS="$BASE_RPS"
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
# MULTIEDGE_RESULTS_DIR: run_multiedge_batch.sh がレジューム判定用に固定パスを
# 指定するためのオプショナル上書き（未指定時は従来通りタイムスタンプ付き自動生成、
# 既存の単発実行インターフェースは無変更）。
RESULTS_DIR="${MULTIEDGE_RESULTS_DIR:-$PROJECT_ROOT/results/multiedge_${TIMESTAMP}_N${EDGE_COUNT}_${EDGE_MODE}}"
mkdir -p "$RESULTS_DIR"

echo "============================================"
echo " 合成マルチエッジ・スケーラビリティ検証"
echo " $TIMESTAMP"
echo "============================================"
echo " EDGE_COUNT=$EDGE_COUNT  EDGE_MODE=$EDGE_MODE"
echo " BASE_RPS=$BASE_RPS  PER_EDGE_RPS=$PER_EDGE_RPS  (合計目標RPS: $(( PER_EDGE_RPS * EDGE_COUNT )))"
echo " PATTERN=$PATTERN  DURATION=${DURATION}s"
echo " TARGET=$PROPOSED_URL"
echo "============================================"
echo ""

EDGE_PIDS=()
MON_PID=""
_EXIT_CODE=0
_INTERRUPTED=false
cleanup() {
    for pid in "${EDGE_PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    kill "${MON_PID:-}" 2>/dev/null || true
    for pid in "${EDGE_PIDS[@]:-}"; do
        wait "$pid" 2>/dev/null || true
    done
    wait "${MON_PID:-}" 2>/dev/null || true
    if [ "$_INTERRUPTED" = true ] || [ "$_EXIT_CODE" -ne 0 ]; then
        local reason="終了コード: $_EXIT_CODE"
        [ "$_INTERRUPTED" = true ] && reason="Ctrl+C または SIGTERM により中断"
        echo "[OBS FAILED] マルチエッジ実験が失敗しました: ${TIMESTAMP}（理由: ${reason}、結果ディレクトリ: ${RESULTS_DIR}）"
    fi
}
trap '_EXIT_CODE=$?; cleanup' EXIT
trap '_INTERRUPTED=true; exit 130' INT TERM

# --- Step 1: ヘルスチェック ---
echo "[1/6] ヘルスチェック..."
health_ok "${PROPOSED_URL%/events}/health" || { echo "[ERROR] Proposed node not reachable"; exit 1; }
echo "  Proposed: OK"
echo ""

# --- Step 2: リソースモニター起動（proposed側のみ、本実験は中央受付層のスケーラビリティが対象） ---
echo "[2/6] リソースモニター起動..."
_ssh_args_proposed=()
if [ -n "${PROPOSED_IP:-}" ]; then
    _ssh_args_proposed+=(--ssh-host "${PROPOSED_IP}" --ssh-user "${PROPOSED_SSH_USER:-root}" --ssh-port "${PROPOSED_SSH_PORT:-22}")
fi
python3 "$PROJECT_ROOT/src/monitor/resource.py" \
    --compose-file "$PROJECT_ROOT/proposed/docker-compose.yml" \
    "${_ssh_args_proposed[@]}" \
    --output "$RESULTS_DIR/resource_proposed.csv" \
    --duration $((DURATION + 30)) &
MON_PID=$!
echo "  Proposed monitor PID: $MON_PID (mode: ${PROPOSED_IP:-local})"
echo ""

# --- Step 3: N個の仮想エッジを並行起動 ---
echo "[3/6] 仮想エッジ ${EDGE_COUNT} 個を並行起動 (per-edge rps=$PER_EDGE_RPS)..."
for i in $(seq 1 "$EDGE_COUNT"); do
    python3 "$PROJECT_ROOT/src/generator/spike.py" \
        --targets "$PROPOSED_URL" \
        --rps "$PER_EDGE_RPS" \
        --duration "$DURATION" \
        --pattern "$PATTERN" \
        --output "$RESULTS_DIR/inject_log_edge${i}.csv" &
    EDGE_PIDS+=($!)
done
echo "  起動したPID: ${EDGE_PIDS[*]}"

for pid in "${EDGE_PIDS[@]}"; do
    wait "$pid"
done
echo "  全仮想エッジ完了"
echo ""

# --- Step 4: 送信ログをマージ ---
echo "[4/6] 送信ログをマージ..."
MERGED_LOG="$RESULTS_DIR/inject_log.csv"
head -n 1 "$RESULTS_DIR/inject_log_edge1.csv" > "$MERGED_LOG"
for i in $(seq 1 "$EDGE_COUNT"); do
    tail -n +2 "$RESULTS_DIR/inject_log_edge${i}.csv" >> "$MERGED_LOG"
done
MERGED_LINES=$(($(wc -l < "$MERGED_LOG") - 1))
echo "  マージ済み: ${MERGED_LINES}行 → $MERGED_LOG"
echo ""

# --- Step 5: データ伝搬待機 → 欠損率計測 ---
echo "[5/6] データ伝搬待機..."
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

echo "  欠損率計測..."
python3 "$PROJECT_ROOT/src/measure/loss_rate.py" \
    --inject-log "$MERGED_LOG" \
    --clickhouse "$CLICKHOUSE_URL" \
    --elasticsearch "" \
    --proposed-target "$PROPOSED_URL" \
    --output "$RESULTS_DIR/loss_rate.json"
echo ""

# --- Step 6: メタデータ保存 & モニター停止 ---
echo "[6/6] メタデータ保存 & モニター停止..."
python3 - "$RESULTS_DIR/meta.json" "$EDGE_COUNT" "$EDGE_MODE" "$BASE_RPS" "$PER_EDGE_RPS" "$PATTERN" "$DURATION" "$TIMESTAMP" <<'PYEOF'
import json
import sys

path, edge_count, edge_mode, base_rps, per_edge_rps, pattern, duration, timestamp = sys.argv[1:9]
with open(path, "w") as f:
    json.dump(
        {
            "edge_count": int(edge_count),
            "edge_mode": edge_mode,
            "base_rps": int(base_rps),
            "per_edge_rps": int(per_edge_rps),
            "pattern": pattern,
            "duration": int(duration),
            "timestamp": timestamp,
        },
        f,
        indent=2,
    )
PYEOF

kill "$MON_PID" 2>/dev/null || true
wait "$MON_PID" 2>/dev/null || true

echo ""
echo "============================================"
echo " 実験完了: $RESULTS_DIR"
echo "============================================"
ls -la "$RESULTS_DIR/"

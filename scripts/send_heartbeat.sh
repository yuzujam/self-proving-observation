#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── send_heartbeat.sh — 1分ごとに中央レシーバーへ生存証明を送信する
#
# 通信断・クラッシュによる観測欠損期間を自己証明するための
# Heartbeat レイヤー（内部指針 1.2.2）。失敗しても何もしない
# （= その分だけ heartbeats テーブルに記録が欠け、欠損として可視化される）。
#
# Cron（proposed-node / baseline-node 共通）: * * * * * /path/to/scripts/send_heartbeat.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
load_experiment_env

PROPOSED_URL="${PROPOSED_URL:-http://localhost:8000/events}"
HEARTBEAT_URL="${HEARTBEAT_URL:-${PROPOSED_URL%/events}/heartbeat}"
NODE_ID="${NODE_ID:-proposed}"

curl -sf --max-time 10 -X POST "$HEARTBEAT_URL" \
    -H "Content-Type: application/json" \
    -d "{\"node_id\":\"${NODE_ID}\",\"status\":\"ok\"}" \
    >/dev/null 2>&1

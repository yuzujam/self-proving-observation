#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── validate_pipeline.sh  — 本番実験前の E2E パイプライン検証
#
# 小規模テスト（100 RPS × 30秒）を両パイプラインに送信し、
# 欠損率が 0% であることを確認してから終了する。
# 失敗した場合は exit 1 で止まる（run_full_thesis.sh の --preflight として使用可能）。
#
# 使い方:
#   bash scripts/validate_pipeline.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"
load_experiment_env

BASELINE_URL="${BASELINE_URL:-http://localhost:5080}"
PROPOSED_URL="${PROPOSED_URL:-http://localhost:8000/events}"
CLICKHOUSE_URL="${CLICKHOUSE_URL:-http://localhost:8123}"
ELASTICSEARCH_URL="${ELASTICSEARCH_URL:-http://localhost:9200}"
# CLICKHOUSE_USER未設定時は空配列（認証ヘッダーなし）になり、無認証
# ClickHouseに対する既存の挙動を変えない（非破壊的拡張）。
CH_AUTH_ARGS=()
if [[ -n "${CLICKHOUSE_USER:-}" ]]; then
    CH_AUTH_ARGS=(-H "X-ClickHouse-User: ${CLICKHOUSE_USER}" -H "X-ClickHouse-Key: ${CLICKHOUSE_PASSWORD:-}")
fi

VALIDATE_DIR="$PROJECT_ROOT/results/validate_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$VALIDATE_DIR"

ok() { echo "  [OK] $*"; }
ng() { echo "  [NG] $*" >&2; }
die() { echo "[FATAL] $*" >&2; exit 1; }

echo "============================================"
echo " パイプライン E2E 検証"
echo "============================================"

# ── Step 1: ヘルスチェック ──
echo "[1/4] ヘルスチェック..."

health_ok "${PROPOSED_URL%/events}/health" \
    && ok "Proposed receiver (${PROPOSED_URL%/events}/health)" \
    || die "Proposed receiver に接続できません"

health_ok "$ELASTICSEARCH_URL/_cluster/health" \
    && ok "Baseline Elasticsearch ($ELASTICSEARCH_URL)" \
    || die "Baseline Elasticsearch に接続できません"

health_ok "$CLICKHOUSE_URL" "${CH_AUTH_ARGS[@]}" \
    && ok "ClickHouse ($CLICKHOUSE_URL)" \
    || die "ClickHouse に接続できません"

health_ok "$BASELINE_URL" \
    && ok "Baseline Logstash ($BASELINE_URL)" \
    || die "Baseline Logstash に接続できません"

# ── Step 2: テスト送信（100 RPS × 30秒 = 3,000イベント） ──
echo ""
echo "[2/4] テスト送信（100 RPS × 30秒）..."
python3 "$PROJECT_ROOT/src/generator/spike.py" \
    --targets "$BASELINE_URL" "$PROPOSED_URL" \
    --rps 100 \
    --duration 30 \
    --pattern flat \
    --output "$VALIDATE_DIR/inject_log.csv"

_proposed_host=$(python3 -c "from urllib.parse import urlparse; p=urlparse('$PROPOSED_URL'); print(p.hostname or p.path.split(':')[0])")
_baseline_host=$(python3 -c "from urllib.parse import urlparse; p=urlparse('$BASELINE_URL'); print(p.hostname or p.path.split(':')[0])")

SENT_PROPOSED=$(python3 -c "
import csv
with open('$VALIDATE_DIR/inject_log.csv') as f:
    rows = [r for r in csv.DictReader(f) if r.get('sent_ok','1')=='1' and r['target'].split(':')[0]=='$_proposed_host']
print(len(rows))
" 2>/dev/null || echo "0")

SENT_BASELINE=$(python3 -c "
import csv
with open('$VALIDATE_DIR/inject_log.csv') as f:
    rows = [r for r in csv.DictReader(f) if r.get('sent_ok','1')=='1' and r['target'].split(':')[0]=='$_baseline_host']
print(len(rows))
" 2>/dev/null || echo "0")

echo "  送信完了: proposed=${SENT_PROPOSED}件 / baseline=${SENT_BASELINE}件"

# ── Step 3: キュードレイン待機（最大5分） ──
echo ""
echo "[3/4] Redisキュードレイン待機..."
MAX_WAIT=300
waited=0
while [ $waited -lt $MAX_WAIT ]; do
    queue_len=$(queue_length "${PROPOSED_URL%/events}/health")
    if [ "$queue_len" = "0" ]; then
        echo "  キュー空 (${waited}s) → +5s バッファ"
        sleep 5
        break
    fi
    sleep 5
    waited=$(( waited + 5 ))
    echo "  キュー残: ${queue_len}件 (${waited}s)..."
done
[ $waited -ge $MAX_WAIT ] && die "キュードレインタイムアウト (${MAX_WAIT}s)"

# ── Step 4: 欠損率計測・合否判定 ──
echo ""
echo "[4/4] 欠損率計測..."
python3 "$PROJECT_ROOT/src/measure/loss_rate.py" \
    --inject-log "$VALIDATE_DIR/inject_log.csv" \
    --clickhouse "$CLICKHOUSE_URL" \
    --elasticsearch "$ELASTICSEARCH_URL" \
    --proposed-target "$PROPOSED_URL" \
    --baseline-target "$BASELINE_URL" \
    --output "$VALIDATE_DIR/loss_rate.json"

# 結果判定
PROPOSED_LOSS=$(python3 -c "
import json
with open('$VALIDATE_DIR/loss_rate.json') as f:
    d = json.load(f)
print(d.get('proposed',{}).get('loss_rate_percent','N/A'))
" 2>/dev/null || echo "N/A")

BASELINE_LOSS=$(python3 -c "
import json
with open('$VALIDATE_DIR/loss_rate.json') as f:
    d = json.load(f)
print(d.get('baseline',{}).get('loss_rate_percent','N/A'))
" 2>/dev/null || echo "N/A")

echo ""
echo "============================================"
echo " 検証結果"
echo "============================================"

PASS=true

if [ "$PROPOSED_LOSS" = "0.0" ] || [ "$PROPOSED_LOSS" = "0" ]; then
    ok "Proposed 欠損率: ${PROPOSED_LOSS}%"
else
    ng "Proposed 欠損率: ${PROPOSED_LOSS}% (期待値: 0%)"
    PASS=false
fi

if [ "$BASELINE_LOSS" != "N/A" ]; then
    echo "  [INFO] Baseline 欠損率: ${BASELINE_LOSS}% (ELKはスパイク時に欠損する設計)"
else
    ng "Baseline 欠損率: 計測できませんでした（Logstash→ES パイプライン要確認）"
    PASS=false
fi

echo "  ログ: $VALIDATE_DIR/"
echo "============================================"

if [ "$PASS" = true ]; then
    echo " [PASS] パイプライン正常。本番実験を開始できます。"
    echo " 実行: bash scripts/run_full_thesis.sh --skip-infra"
else
    die "パイプライン検証に失敗しました。上記を修正してから実験を開始してください。"
fi

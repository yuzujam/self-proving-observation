#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── run_detached.sh  — SSH切断後も実験を継続するバックグラウンド起動
#
# 使い方:
#   bash scripts/run_detached.sh                    # tmux優先、なければnohup
#   bash scripts/run_detached.sh --resume           # 中断からの再開
#   bash scripts/run_detached.sh --skip-infra       # インフラ起動をスキップ
#   bash scripts/run_detached.sh --nohup            # tmuxを使わずnohupで起動
#
# 進捗確認:
#   tmux attach -t obs          # tmux セッションに再接続
#   tail -f results/thesis_*/thesis_experiment.log  # ログを流し見

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# ── 引数パース ──
USE_NOHUP=false
PASSTHROUGH_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --nohup) USE_NOHUP=true ;;
        *) PASSTHROUGH_ARGS+=("$arg") ;;
    esac
done

CMD="bash $SCRIPT_DIR/run_full_thesis.sh ${PASSTHROUGH_ARGS[*]:-}"

# ── tmux で起動（優先） ──
if [ "$USE_NOHUP" = false ] && command -v tmux > /dev/null 2>&1; then
    SESSION="obs"

    if tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "[ERROR] tmux セッション '$SESSION' がすでに存在します。"
        echo "  再接続:  tmux attach -t $SESSION"
        echo "  強制終了: tmux kill-session -t $SESSION"
        exit 1
    fi

    tmux new-session -d -s "$SESSION" -x 220 -y 50 "$CMD"

    echo "============================================"
    echo " 実験をバックグラウンドで起動しました"
    echo " セッション名: $SESSION"
    echo "============================================"
    echo ""
    echo " 再接続:"
    echo "   tmux attach -t $SESSION"
    echo ""
    echo " ログ確認（別ターミナルから）:"
    echo "   tail -f $PROJECT_ROOT/results/thesis_*/thesis_experiment.log"
    echo ""
    echo " 強制停止:"
    echo "   tmux kill-session -t $SESSION"
    echo "============================================"
    exit 0
fi

# ── nohup で起動（tmuxがない場合 or --nohup指定） ──
LOG_DIR="$PROJECT_ROOT/results"
mkdir -p "$LOG_DIR"
NOHUP_LOG="$LOG_DIR/nohup_$(date +%Y%m%d_%H%M%S).log"

nohup bash -c "$CMD" > "$NOHUP_LOG" 2>&1 &
PID=$!

echo "============================================"
echo " 実験をバックグラウンドで起動しました"
echo " PID: $PID"
echo " ログ: $NOHUP_LOG"
echo "============================================"
echo ""
echo " ログ確認:"
echo "   tail -f $NOHUP_LOG"
echo ""
echo " プロセス確認:"
echo "   ps aux | grep run_full_thesis"
echo ""
echo " 強制停止:"
echo "   kill $PID"
echo "============================================"

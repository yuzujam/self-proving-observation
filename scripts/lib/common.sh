# self-proving-observation/
# └── scripts/
#     └── lib/
#         └── common.sh  — scripts/*.sh 間で重複していた定型処理の共通化
#
# source して使う（実行はしない）。呼び出し元の `set -euo pipefail` や
# エラーハンドリング方針に影響しないよう、ここでは set を一切変更しない。
# 通知の要否・失敗時に止まるか続けるかといった各スクリプト固有の挙動は
# 呼び出し元にそのまま残し、「同じ入力なら同じ出力を返す」部分だけを
# 共通化する。

# cron 実行時は PATH が最小限（/usr/bin:/bin 等）になり、snap や
# ユーザーローカルにインストールした rclone 等が見つからず失敗する
# （対話シェルでは動くが cron からは失敗する典型パターン）。
#
# $HOME/bin を /snap/bin より前に置く: snap 版コマンドは
# cron.service の cgroup が snap の confinement 要件を満たさず
# "not a snap cgroup for tag snap.<name>.<name>" で実行自体が
# 拒否されることがある（baseline-node の rclone で確認済み）。
# ユーザーローカルにスタンドアロン版バイナリを置いた場合はそちらを
# 優先させ、confinement の問題を回避できるようにする。
export PATH="$HOME/bin:$PATH:/snap/bin"

# 実験設定ファイルを読み込む。呼び出し元で SCRIPT_DIR を定義しておくこと。
# 第1引数に "verbose" を渡すと読み込みメッセージを表示する
# （run_batch_resumable.sh 互換）。
load_experiment_env() {
    local env_file="${SCRIPT_DIR}/experiment.env"
    if [ -f "$env_file" ]; then
        set -a
        source "$env_file"
        set +a
        [ "${1:-}" = "verbose" ] && echo "[CONFIG] $env_file を読み込みました"
    fi

    # cron実行時は対話シェルのvenv activateを経由しないため、python3が
    # システム標準（numpy/scipy/torch/shap/matplotlib未インストール）を
    # 指してしまい、run_full_thesis.shのプリフライトが常に失敗する不具合が
    # あった（2026-07-07確認）。
    # experiment.envに以下を設定すると、cron経由でもvenvのpython3が優先される:
    #   export OBS_VENV=/home/<user>/obs-venv
    if [ -n "${OBS_VENV:-}" ] && [ -x "${OBS_VENV}/bin/python3" ]; then
        export PATH="${OBS_VENV}/bin:$PATH"
    fi
}

# ヘルスチェック URL に到達可能かどうかを終了ステータスで返す。
# --connect-timeout/--max-time を明示しないと、応答が返らない相手先に対して
# curlが無期限にハングし、cron経由の呼び出し元スクリプト全体を止めてしまう
# （2026-07-07、対照実験が7日間完走しない件の調査で発見。「DB接続にはtimeout指定」と
# 同じ考え方をヘルスチェックにも適用する）。
# 第2引数以降は追加のcurlオプション（例: ClickHouse認証ヘッダー）として
# そのままcurlへ渡す。既存の1引数呼び出しは無変更で動作する。
health_ok() {
    local url="$1"; shift
    curl -sf --connect-timeout 5 --max-time 10 "$@" "$url" > /dev/null 2>&1
}

# Proposed レシーバーの /health レスポンスから queue_length を取得する。
# 取得できない場合は "1"（未ドレイン扱い）を返す。
queue_length() {
    curl -sf --connect-timeout 5 --max-time 10 "$1" 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('queue_length',1))" 2>/dev/null \
        || echo "1"
}

# ディスク使用率を "使用量/合計 (使用率%)" 形式で返す。
disk_usage_summary() {
    df -h / | tail -1 | awk '{print $3 "/" $2 " (" $5 ")"}'
}

# ディスク使用率を整数（%記号なし）で返す。しきい値比較用。
disk_usage_percent() {
    df -P / | tail -1 | awk '{print $5}' | tr -d '%'
}

#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── rotate_and_backup.sh  — ログローテート & S3-compatible object storage バックアップ
#
# 処理フロー:
#   1. results/          → S3-compatible object storage に差分同期
#   2. 古い T-Pot ES インデックス → 1分集計CSV に変換 → gzip → S3-compatible object storage → ES から削除
#   3. ディスク使用率をログ出力

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"

# baseline-node は自ノードに ClickHouse を持たないため、crontab
# （scripts/expected-crontab/baseline.txt）では
# `CLICKHOUSE_URL=http://<PROPOSED_IP>:8123 rotate_and_backup.sh` のように
# 呼び出し元が都度 proposed-node の URL を明示指定する運用になっている。
# experiment.env にも CLICKHOUSE_URL の記載があり得るため、読み込みで
# 上書きされないよう呼び出し元の値を退避しておき、読み込み後に復元する。
_CLICKHOUSE_URL_FROM_CALLER="${CLICKHOUSE_URL:-}"
# 素の`source`ではexperiment.envが`export`なしで書かれていた場合に子プロセス
# （record_backup_result.py の CLICKHOUSE_USER/PASSWORD）へ伝わらない（#GG・#II）。
# 他のスクリプトと同じく自動exportする共通関数を使う（doc/known-limitations.md #CCC）。
load_experiment_env
if [[ -n "$_CLICKHOUSE_URL_FROM_CALLER" ]]; then
    CLICKHOUSE_URL="$_CLICKHOUSE_URL_FROM_CALLER"
fi

REMOTE="<rclone-remote>"
BUCKET="<bucket>"
# extract_features.py をサブプロセスとして呼ぶため export する
# （export しないと experiment.env で上書きしても子プロセスに伝わらない）
export ES_URL="${ES_URL:-http://localhost:64298}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"
RESULTS_DIR="${PROJECT_DIR}/results"
FEATURES_DIR="${PROJECT_DIR}/data/features"
LOG_FILE="${PROJECT_DIR}/logs/rotate_and_backup.log"
export BACKUP_DATE="${BACKUP_DATE:-$(date '+%Y-%m-%d')}"
export NODE_ID="${NODE_ID:-baseline}"

mkdir -p "$FEATURES_DIR" "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# 個々の失敗では通知せず、実行全体の最後に失敗があった場合のみ
# 1通通知するため蓄積する（backup_clickhouse.shと同じ方針）
FAILURES=()

# backup_log テーブルへ成否を記録（ClickHouse が利用可能な場合のみ）
# $1: テーブル/対象名  $2: status  $3: row_count  $4: error_message
#
# doc/known-limitations.md #FF: 以前は`2>/dev/null || true`でexit code・
# stderr双方を無条件に握りつぶしており、backup_log記録自体の失敗が
# FAILURES/通知のどちらにも一切現れなかった（CLICKHOUSE_URL未設定時の
# 意図的スキップとは区別できていなかった）。URL未設定時のスキップは維持し
# つつ、URL設定済みなのに記録が失敗した場合のみFAILURESに積む。
record_backup_result() {
    [[ -z "${CLICKHOUSE_URL:-}" ]] && return 0
    local target="$1" status="$2" rows="${3:-0}" err="${4:-}"
    if ! python3 "$SCRIPT_DIR/record_backup_result.py" \
        --table "$target" --status "$status" --rows "$rows" --error "$err" \
        --backup-date "$BACKUP_DATE" --node-id "$NODE_ID" \
        --clickhouse-url "${CLICKHOUSE_URL:-}" --quiet \
        2>/dev/null; then
        FAILURES+=("${target}: backup_logへの記録に失敗（status=${status}を記録できず、doc/known-limitations.md #FF）")
    fi
}

# T-Pot ESインデックスを1分集計CSV(gzip)へ変換する。成功時のみ0を返す。
# $1: インデックス名  $2: 出力gzipパス
#
# パイプライン `python3 extract_features.py | gzip > f` の終了ステータスは末尾の
# gzipのもので、pipefailなしではextract_features.pyの失敗（ES障害等）がマスク
# される。さらにgzipは空入力でも20バイトの有効なファイルを作るため `[[ -s f ]]`
# も通り、「抽出失敗→空のバックアップをアップロード→ESインデックスを不可逆に
# 削除」という経路が成立していた（doc/known-limitations.md #RR）。ここでは
#   1. 抽出プロセス自体の終了ステータス（pipefail）
#   2. 展開後にヘッダ行+データ行が1行以上あること
# の両方を満たした場合のみ成功とする。
extract_index_to_gz() {
    local idx="$1" out="$2"
    ( set -o pipefail; python3 "${SCRIPT_DIR}/extract_features.py" "$idx" 2>>"$LOG_FILE" \
        | gzip > "$out" ) || return 1
    [[ -s "$out" ]] || return 1
    local nlines
    nlines=$(gzip -dc "$out" 2>/dev/null | wc -l | tr -d '[:space:]')
    [[ "${nlines:-0}" -ge 2 ]]
}

log "=== rotate_and_backup 開始 ==="
log "ディスク使用率: $(disk_usage_summary)"

# ----------------------------------------------------------------
# 1. results/ を S3-compatible object storage にアップロード
#    rclone syncではなくcopyを使う。proposed-nodeとbaseline-nodeが
#    同じ S3-compatible object storage 上の results/ パスを共有しており、syncは「destを
#    sourceに合わせる」ため自ノードのローカルに存在しないファイルを
#    destから削除してしまう。2026-07-13、baseline-nodeでの手動実行が
#    proposed-node分のバックアップ済みデータ（fidelity結果含む
#    10.49GiB・1460ファイル）をS3-compatible object storageから消してしまう事故が発生した。
#    copyは追加・更新のみで削除しないため、複数ノードが同じ宛先へ
#    書き込んでも互いのデータを消さない。
# ----------------------------------------------------------------
log "[BACKUP] results/ → ${REMOTE}:${BUCKET}/results/"
if rclone copy "$RESULTS_DIR" "${REMOTE}:${BUCKET}/results/" \
    --log-level INFO --log-file "$LOG_FILE" 2>&1; then
    log "[BACKUP] 同期完了"
    record_backup_result "results_sync" "success" 0

    # RESULTS_RETAIN_DAYS が設定されている場合のみ、S3-compatible object storage に同期済みの
    # 古い results/batch_*・thesis_*・ablation_*・multiedge_* ディレクトリを
    # ローカルから削除する（未設定時は従来通り何もしない = 既存ノードの挙動を変えない）。
    # 対照実験バッチ（results/batch_YYYYMMDD_*）はローカルに残り続けると
    # 1回あたり数十GBに達し得るため、中央ノードでのディスク枯渇を防ぐ。
    # ablation_*・multiedge_*（doc/pipeline-spec.md「補強実験」節、2026-09-03追加）は
    # 単発実行のディレクトリ名がプレフィックス1語（例: ablation_20260903_...）である一方、
    # バッチ実行の入れ物ディレクトリはプレフィックス2語（例: ablation_batch_20260903_...）
    # であり、旧来の「最初の_までを削って先頭8文字」という抽出方法ではbatch側の日付を
    # 取り出せない。日付8桁を前後を_で挟んだパターンとして正規表現で抽出する方式に
    # 変更し、既存のbatch_*/thesis_*も含め全命名パターンに対応させた（後方互換確認済み）。
    if [[ -n "${RESULTS_RETAIN_DAYS:-}" ]]; then
        CUTOFF_RESULTS=$(date -d "-${RESULTS_RETAIN_DAYS} days" '+%Y%m%d')
        log "[RESULTS] ${RESULTS_RETAIN_DAYS}日より古いローカルbatch/thesis/ablation/multiedgeディレクトリを圧縮バックアップ後に削除（基準: ${CUTOFF_RESULTS}）"
        for d in "$RESULTS_DIR"/batch_* "$RESULTS_DIR"/thesis_* "$RESULTS_DIR"/ablation_* "$RESULTS_DIR"/multiedge_*; do
            [[ -d "$d" ]] || continue
            dname=$(basename "$d")
            [[ "$dname" =~ _([0-9]{8})_ ]] || continue
            dir_date="${BASH_REMATCH[1]}"
            if [[ "$dir_date" < "$CUTOFF_RESULTS" ]]; then
                # 上の rclone copy は results/ 全体を1コマンドでアップロードするため、
                # 帯域不足等で個々のディレクトリの転送が終わっていなくても
                # 全体の exit code だけでは検出できない。削除前にこの
                # ディレクトリ単体を圧縮アーカイブとして確実にバックアップし
                # 直し、成功した場合のみ削除する（生データ消失の最終防衛線）。
                archive="/tmp/${dname}.tar.gz"
                if tar -czf "$archive" -C "$RESULTS_DIR" "$dname" \
                    && rclone copy "$archive" "${REMOTE}:${BUCKET}/results-archive/" \
                        --log-level INFO --log-file "$LOG_FILE" 2>&1; then
                    rm -f "$archive"
                    rm -rf "$d"
                    log "[RESULTS] 圧縮バックアップ後に削除: ${dname}"
                    record_backup_result "results_archive_${dname}" "success" 0
                else
                    rm -f "$archive"
                    log "[RESULTS] 圧縮バックアップ失敗。安全のため削除をスキップ: ${dname}"
                    record_backup_result "results_archive_${dname}" "upload_failed" 0 "tar/rclone failed"
                fi
            fi
        done
    fi

    # ------------------------------------------------------------
    # 1b. ディスク使用率が高い場合、RESULTS_RETAIN_DAYSの日数に達して
    #     いなくても、当日分を除く古いbatch/thesisディレクトリを追加で
    #     圧縮バックアップ後に削除する（DISK_ROTATE_THRESHOLD未設定時は
    #     従来通り何もしない=既存ノードの挙動を変えない）。
    #     RESULTS_RETAIN_DAYS=2・1日1回(23:00)実行だけでは、batch_*
    #     (1回約27GB)の生成ペースに追いつかず、毎時のcheck_disk_usage.sh
    #     による検知と1日1回の解放の間の空白時間帯にディスク使用率が
    #     85%に達した（proposed-node、2026-07-17）。2b(ESインデックス
    #     向けの同種の安全弁)と同じ考え方をresults/にも適用する。
    #     doc/known-limitations.md参照。
    # ------------------------------------------------------------
    if [[ -n "${DISK_ROTATE_THRESHOLD:-}" ]]; then
        RESULTS_MIN_RETAIN_DAYS="${RESULTS_MIN_RETAIN_DAYS:-1}"
        RESULTS_MIN_CUTOFF=$(date -d "-${RESULTS_MIN_RETAIN_DAYS} days" '+%Y%m%d')
        EXTRA_RESULTS_DELETED=0
        CURRENT_USAGE=$(disk_usage_percent)
        log "[DISK-RESULTS] 使用率${CURRENT_USAGE}%（しきい値${DISK_ROTATE_THRESHOLD}%、最低保持${RESULTS_MIN_RETAIN_DAYS}日）"

        while [[ "$CURRENT_USAGE" -ge "$DISK_ROTATE_THRESHOLD" ]]; do
            OLDEST_DIR=""
            OLDEST_DATE=""
            for d in "$RESULTS_DIR"/batch_* "$RESULTS_DIR"/thesis_* "$RESULTS_DIR"/ablation_* "$RESULTS_DIR"/multiedge_*; do
                [[ -d "$d" ]] || continue
                dname=$(basename "$d")
                [[ "$dname" =~ _([0-9]{8})_ ]] || continue
                dir_date="${BASH_REMATCH[1]}"
                [[ "$dir_date" < "$RESULTS_MIN_CUTOFF" ]] || continue
                if [[ -z "$OLDEST_DATE" || "$dir_date" < "$OLDEST_DATE" ]]; then
                    OLDEST_DATE="$dir_date"
                    OLDEST_DIR="$d"
                fi
            done
            [[ -z "$OLDEST_DIR" ]] && { log "[DISK-RESULTS] 追加削除対象なし。打ち切り"; break; }

            dname=$(basename "$OLDEST_DIR")
            archive="/tmp/${dname}.tar.gz"
            log "[DISK-RESULTS] 追加削除: ${dname}"
            if tar -czf "$archive" -C "$RESULTS_DIR" "$dname" \
                && rclone copy "$archive" "${REMOTE}:${BUCKET}/results-archive/" \
                    --log-level INFO --log-file "$LOG_FILE" 2>&1; then
                rm -f "$archive"
                rm -rf "$OLDEST_DIR"
                log "[DISK-RESULTS] 圧縮バックアップ後に削除: ${dname}"
                EXTRA_RESULTS_DELETED=$((EXTRA_RESULTS_DELETED + 1))
                record_backup_result "results_archive_${dname}" "success" 0
            else
                rm -f "$archive"
                log "[DISK-RESULTS] ${dname}の圧縮バックアップに失敗。安全のため打ち切り"
                record_backup_result "results_archive_${dname}" "upload_failed" 0 "disk-based results rotation backup failed"
                break
            fi
            CURRENT_USAGE=$(disk_usage_percent)
        done
        [[ "$EXTRA_RESULTS_DELETED" -gt 0 ]] && log "[DISK-RESULTS] 追加削除で${EXTRA_RESULTS_DELETED}件削除、使用率${CURRENT_USAGE}%まで回復"
    fi
else
    log "[BACKUP] 同期失敗（継続）"
    record_backup_result "results_sync" "upload_failed" 0 "rclone sync failed"
fi

# ----------------------------------------------------------------
# 2. 古い T-Pot ES インデックスをローテート
#    抽出 → gzip → S3-compatible object storage → ES 削除
# ----------------------------------------------------------------
CUTOFF_DATE=$(date -d "-${RETAIN_DAYS} days" '+%Y.%m.%d')
log "[ES] ${RETAIN_DAYS}日より古いインデックスを処理（基準: logstash-${CUTOFF_DATE}）"

DELETED=0
FAILED=0

while IFS= read -r idx; do
    [[ -z "$idx" || "$idx" != logstash-* ]] && continue
    idx_date="${idx#logstash-}"

    # 日付比較（YYYY.MM.DD は辞書順で正しく比較できる）
    if [[ "$idx_date" < "$CUTOFF_DATE" ]]; then
        csv_date="${idx_date//./-}"
        local_gz="${FEATURES_DIR}/features_${csv_date}.csv.gz"

        log "[EXTRACT] $idx を1分集計CSV に変換中..."

        # 特徴量抽出 → gzip 圧縮（失敗・空出力・ヘッダのみは失敗扱い、#RR）
        if extract_index_to_gz "$idx" "$local_gz"; then

            gz_size=$(du -sh "$local_gz" | awk '{print $1}')
            log "[EXTRACT] 変換完了: ${local_gz} (${gz_size})"

            # S3-compatible object storage にアップロード
            if rclone copy "$local_gz" "${REMOTE}:${BUCKET}/features/" \
                --log-level INFO --log-file "$LOG_FILE" 2>&1; then
                log "[UPLOAD] 完了: features_${csv_date}.csv.gz → S3-compatible object storage"

                # ES インデックス削除
                ack=$(curl -s --connect-timeout 5 --max-time 30 -X DELETE "${ES_URL}/${idx}" \
                    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('acknowledged','?'))" 2>/dev/null)
                log "[ES] 削除: ${idx} (acknowledged=${ack})"
                rm -f "$local_gz"
                DELETED=$((DELETED + 1))
                record_backup_result "$idx" "success" 0
            else
                log "[UPLOAD] 失敗: ${idx} は保持（次回リトライ）"
                FAILED=$((FAILED + 1))
                record_backup_result "$idx" "upload_failed" 0 "rclone copy failed"
            fi
        else
            log "[EXTRACT] 失敗: ${idx} は保持"
            rm -f "$local_gz"
            FAILED=$((FAILED + 1))
            record_backup_result "$idx" "export_failed" 0 "extract_features.py failed"
        fi
    fi
done < <(curl -s --connect-timeout 5 --max-time 30 "${ES_URL}/_cat/indices/logstash-*?h=index" 2>/dev/null)

log "[ES] ローテート完了: 削除=${DELETED}件 失敗=${FAILED}件"

# ----------------------------------------------------------------
# 2b. ディスク使用率が高い場合、RETAIN_DAYSの日数に達していなくても
#     MIN_RETAIN_DAYSまでは追加でローテートする
#     （DISK_ROTATE_THRESHOLD未設定時は従来通り何もしない=既存ノードの挙動を変えない）。
#     日数固定のローテートだけでは、ホスト全体の他要因（honeypotデータ増加等）
#     による急なディスク圧迫に追いつけなかった事故（doc/known-limitations.md #J）を
#     受けて追加した安全弁。抽出→S3-compatible object storageアップロード確認後→ES削除という
#     既存の安全な手順は変えず、対象範囲を動的に広げるだけ。
# ----------------------------------------------------------------
if [[ -n "${DISK_ROTATE_THRESHOLD:-}" ]]; then
    MIN_RETAIN_DAYS="${MIN_RETAIN_DAYS:-3}"
    MIN_CUTOFF_DATE=$(date -d "-${MIN_RETAIN_DAYS} days" '+%Y.%m.%d')
    EXTRA_DELETED=0
    CURRENT_USAGE=$(disk_usage_percent)
    log "[DISK] 使用率${CURRENT_USAGE}%（しきい値${DISK_ROTATE_THRESHOLD}%、最低保持${MIN_RETAIN_DAYS}日）"

    while [[ "$CURRENT_USAGE" -ge "$DISK_ROTATE_THRESHOLD" ]]; do
        OLDEST_IDX=$(curl -s --connect-timeout 5 --max-time 30 "${ES_URL}/_cat/indices/logstash-*?h=index" 2>/dev/null \
            | grep '^logstash-' | sort | head -1)
        [[ -z "$OLDEST_IDX" ]] && { log "[DISK] 削除対象インデックスなし。打ち切り"; break; }

        idx_date="${OLDEST_IDX#logstash-}"
        if [[ ! "$idx_date" < "$MIN_CUTOFF_DATE" ]]; then
            log "[DISK] 最古のインデックス(${OLDEST_IDX})がMIN_RETAIN_DAYS(${MIN_RETAIN_DAYS}日)以内。これ以上削除しない"
            break
        fi

        csv_date="${idx_date//./-}"
        local_gz="${FEATURES_DIR}/features_${csv_date}.csv.gz"
        log "[DISK] 追加ローテート: ${OLDEST_IDX}"
        if extract_index_to_gz "$OLDEST_IDX" "$local_gz" \
            && rclone copy "$local_gz" "${REMOTE}:${BUCKET}/features/" --log-level INFO --log-file "$LOG_FILE" 2>&1; then
            ack=$(curl -s --connect-timeout 5 --max-time 30 -X DELETE "${ES_URL}/${OLDEST_IDX}" \
                | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('acknowledged','?'))" 2>/dev/null)
            log "[DISK] 削除: ${OLDEST_IDX} (acknowledged=${ack})"
            rm -f "$local_gz"
            EXTRA_DELETED=$((EXTRA_DELETED + 1))
            record_backup_result "$OLDEST_IDX" "success" 0
        else
            log "[DISK] ${OLDEST_IDX} のバックアップに失敗。安全のため打ち切り"
            rm -f "$local_gz"
            record_backup_result "$OLDEST_IDX" "upload_failed" 0 "disk-based rotation backup failed"
            break
        fi
        CURRENT_USAGE=$(disk_usage_percent)
    done
    [[ "$EXTRA_DELETED" -gt 0 ]] && log "[DISK] 追加ローテートで${EXTRA_DELETED}件削除、使用率${CURRENT_USAGE}%まで回復"
fi

# ----------------------------------------------------------------
# 2c. threat-events-*（baseline/logstash/pipeline.conf のHTTP入力が受ける
#     攻撃ジェネレーター〔run_batch_resumable.sh → spike.py〕の合成負荷
#     テストトラフィックの着地先。実際のSuricata検知データではなく、
#     各trialに必要な統計は実行時に loss_rate.json 等へ既に抽出済みのため
#     ES側に生データを保持する価値がない。放置すると無制限に肥大化する
#     （2026-07-13、単一インデックスが98GBまで増加した事故を受けて追加。
#     doc/known-limitations.md 参照）。抽出せず毎回無条件に削除する。
# ----------------------------------------------------------------
while IFS= read -r te_idx; do
    [[ -z "$te_idx" || "$te_idx" != threat-events-* ]] && continue
    ack=$(curl -s --connect-timeout 5 --max-time 30 -X DELETE "${ES_URL}/${te_idx}" \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('acknowledged','?'))" 2>/dev/null)
    log "[THREAT-EVENTS] 削除: ${te_idx} (acknowledged=${ack})"
    record_backup_result "$te_idx" "success" 0
done < <(curl -s --connect-timeout 5 --max-time 30 "${ES_URL}/_cat/indices/threat-events-*?h=index" 2>/dev/null)

# ----------------------------------------------------------------
# 2c-2. baseline/docker-compose.yml が起動する対照実験専用の別Elasticsearch
#     （baseline_es_data ボリューム、既定 http://localhost:9200）向けの
#     threat-events-* 掃除。2cの${ES_URL}はT-Pot本体のESであり、
#     pipeline.confのHTTP入力（ポート5080）が実際に書き込むのはこの
#     compose独立スタックの方のESで、2cのロジックは一度もこちらに
#     到達していなかった（2026-07-16、単一インデックスが103.2GBまで
#     無自覚に肥大化しディスク99%に達した事故を受けて追加。
#     doc/known-limitations.md 参照）。理由は2cと同一（loss_rate.json
#     抽出済みで生データ保持不要）。BASELINE_REF_ES_URL未設定時は
#     従来通り何もしない（=proposed-node・このcompose未起動のノードの
#     挙動を変えない）。
# ----------------------------------------------------------------
if [[ -n "${BASELINE_REF_ES_URL:-}" ]]; then
    while IFS= read -r te_idx; do
        [[ -z "$te_idx" || "$te_idx" != threat-events-* ]] && continue
        ack=$(curl -s --connect-timeout 5 --max-time 30 -X DELETE "${BASELINE_REF_ES_URL}/${te_idx}" \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('acknowledged','?'))" 2>/dev/null)
        log "[THREAT-EVENTS-REF] 削除: ${te_idx} (acknowledged=${ack})"
        record_backup_result "$te_idx" "success" 0
    done < <(curl -s --connect-timeout 5 --max-time 30 "${BASELINE_REF_ES_URL}/_cat/indices/threat-events-*?h=index" 2>/dev/null)
fi

# ----------------------------------------------------------------
# 2d. ES自己申告サイズと実ディスク使用量の乖離を検知
#     2026-07-13、threat-events-2026.07 がクラスタ状態から外れた
#     （dangling registryにも現れない）まま現役プロセスが書き込みを
#     継続し、_cat/indices ベースの通常のローテートでは検知できずに
#     98GBまで無自覚に肥大化した事故があった。同種の異常が再発しても
#     気付けるよう、ESが把握している合計サイズ（disk.indices）と
#     ESデータディレクトリの実サイズを比較し、乖離が大きい場合は
#     通知する（ES_URLが未設定/到達不可の場合は何もしない=既存ノードの
#     挙動を変えない）。
# ----------------------------------------------------------------
if [[ -n "${ES_CONTAINER:-}" ]]; then
    ES_REPORTED_GB=$(curl -s --connect-timeout 5 --max-time 15 "${ES_URL}/_cat/allocation?h=disk.indices" 2>/dev/null \
        | tr -d '[:space:]gGbB')
    # ホスト側cronユーザーはDockerボリュームのマウント先を直接読めない
    # （root権限が必要）ため、コンテナ内のESユーザー権限でdu実行する。
    ES_ACTUAL_GB=$(docker exec "$ES_CONTAINER" du -sBG /usr/share/elasticsearch/data 2>/dev/null | grep -o '^[0-9]*')
    if [[ "$ES_REPORTED_GB" =~ ^[0-9]+$ && "$ES_ACTUAL_GB" =~ ^[0-9]+$ ]]; then
        GAP_GB=$((ES_ACTUAL_GB - ES_REPORTED_GB))
        log "[ES-GAP] ES自己申告=${ES_REPORTED_GB}GB 実ディスク使用=${ES_ACTUAL_GB}GB 差分=${GAP_GB}GB"
        if [[ "$GAP_GB" -ge "${ES_GAP_ALERT_GB:-20}" ]]; then
            log "[ES-GAP] 乖離が${ES_GAP_ALERT_GB:-20}GB以上。クラスタ状態から外れた未把握データの可能性"
            FAILURES+=("ES乖離検知: 自己申告${ES_REPORTED_GB}GB vs 実ディスク${ES_ACTUAL_GB}GB（差${GAP_GB}GB）")
        fi
    fi
fi

# ----------------------------------------------------------------
# 3. data/features/ に残留している未アップロードCSVをリトライ
# ----------------------------------------------------------------
RETRY_COUNT=0
for gz in "${FEATURES_DIR}"/*.csv.gz; do
    [[ -f "$gz" ]] || continue
    fname=$(basename "$gz")
    log "[RETRY] 未アップロードCSV: ${fname}"
    if rclone copy "$gz" "${REMOTE}:${BUCKET}/features/" \
        --log-level INFO --log-file "$LOG_FILE" 2>&1; then
        rm -f "$gz"
        log "[RETRY] 成功: ${fname}"
        RETRY_COUNT=$((RETRY_COUNT + 1))
    fi
done
[[ $RETRY_COUNT -gt 0 ]] && log "[RETRY] ${RETRY_COUNT}件をリトライ成功"

# ----------------------------------------------------------------
# 4. 完了サマリ
# ----------------------------------------------------------------
log "ディスク使用率: $(disk_usage_summary)"
log "=== rotate_and_backup 完了 ==="

if [[ ${#FAILURES[@]} -gt 0 ]]; then
    log "[OBS FAIL] rotate_and_backup 異常検知: ${NODE_ID}"
    log "検知内容:"
    printf '  - %s\n' "${FAILURES[@]}" | while IFS= read -r line; do log "$line"; done
fi

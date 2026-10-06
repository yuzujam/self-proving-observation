#!/bin/bash
# self-proving-observation/
# └── scripts/
#     └── backup_clickhouse.sh  — ClickHouse 日次バックアップ to S3-compatible object storage
#
# 処理フロー:
#   1. threat_events（昨日分）を Parquet エクスポート → S3-compatible object storage
#   2. pipeline_health（昨日分）を Parquet エクスポート → S3-compatible object storage
#   3. heartbeats（昨日分）を Parquet エクスポート → S3-compatible object storage
#   4. 成否を backup_log テーブルに記録（自己証明型完全性保証）
#   5. 直近14日でsuccess記録の無い日（export_failed等）を再バックアップ（catch_up_failed_backups）
#   6. S3-compatible object storage整合性確認済み・7日以上前・件数照合済みの threat_events 日付をローカルから間引く
#   7. 未アップロードのローカル残留ファイルをリトライ
#
# Cron（proposed-node）: 0 2 * * * /path/to/scripts/backup_clickhouse.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

source "$SCRIPT_DIR/lib/common.sh"
# 素の`source`ではexperiment.envが`export`なしで書かれていた場合に子プロセス
# （record_backup_result.py の CLICKHOUSE_USER/PASSWORD）へ伝わらず、backup_logへの
# 記録が認証失敗でサイレントに全滅する（#GG・#II）。他のスクリプトと同じく、
# 自動exportする共通の読み込み関数を使う（doc/known-limitations.md #CCC）。
load_experiment_env

CLICKHOUSE_URL="${CLICKHOUSE_URL:-http://localhost:8123}"
# CLICKHOUSE_USER未設定時は空配列（認証ヘッダーなし）になり、無認証
# ClickHouseに対する既存の挙動を変えない（内部指針 3.4、非破壊的拡張）。
CH_AUTH_ARGS=()
if [[ -n "${CLICKHOUSE_USER:-}" ]]; then
    CH_AUTH_ARGS=(-H "X-ClickHouse-User: ${CLICKHOUSE_USER}" -H "X-ClickHouse-Key: ${CLICKHOUSE_PASSWORD:-}")
fi
REMOTE="${RCLONE_REMOTE:-<rclone-remote>}"
BUCKET="${RCLONE_BUCKET:-<bucket>}"
# 手動で日付を指定した実行（バックフィル等）では過去日の自動再バックアップを走らせない
_BACKUP_DATE_FROM_ENV="${BACKUP_DATE:-}"
export BACKUP_DATE="${BACKUP_DATE:-$(date -d 'yesterday' '+%Y-%m-%d')}"
export NODE_ID="${NODE_ID:-proposed}"
BACKUP_TMP="/tmp/obs-ch-backup-${BACKUP_DATE}"
LOG_FILE="${PROJECT_DIR}/logs/backup_clickhouse.log"

mkdir -p "$BACKUP_TMP" "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# 失敗内容の蓄積（試行・リトライ単位では通知せず、実行全体の最後に
# 失敗があった場合のみ1回通知するため。正常時は通知しない）
FAILURES=()

# backup_log テーブルへ成否を記録（ClickHouse パラメタライズドクエリ）
#
# doc/known-limitations.md #FF: record_backup_result.py のINSERT自体が
# 失敗しても、従来はexit codeを一切確認していなかったため、backup_log
# ——自己証明型完全性保証の台帳そのもの——への記録漏れがFAILURES/通知
# に一切反映されなかった。ここで終了コードを確認し失敗を積む。
record_result() {
    local table="$1" status="$2" rows="${3:-0}" err="${4:-}" bdate="${5:-$BACKUP_DATE}"
    if ! python3 "$SCRIPT_DIR/record_backup_result.py" \
        --table "$table" --status "$status" --rows "$rows" --error "$err" \
        --backup-date "$bdate" --node-id "$NODE_ID" \
        --clickhouse-url "$CLICKHOUSE_URL"; then
        FAILURES+=("${table}: backup_logへの記録に失敗（status=${status}を記録できず、doc/known-limitations.md #FF）")
    fi
}

# テーブルを Parquet エクスポートして S3-compatible object storage へアップロード
# $1: テーブル名  $2: 日付フィルタ用カラム名  $3: 対象日（省略時は BACKUP_DATE）
backup_table() {
    local table="$1" date_col="$2" bdate="${3:-$BACKUP_DATE}"
    local out="${BACKUP_TMP}/${table}_${bdate}.parquet"
    # 同じ表・日付で前回upload_failed等により残った古いParquet。今回のエクスポートが成功
    # したら、後段のリトライ処理がこの古い（後着行を含まない可能性のある）ファイルを
    # 新しいものの上に再アップロードしてしまわないよう消す（doc/known-limitations.md #ZZ）。
    local stale
    stale="$(dirname "$BACKUP_TMP")/obs-ch-backup-${bdate}/${table}_${bdate}.parquet"

    log "[${table}] エクスポート中 (${date_col} = ${bdate})..."

    # curl失敗時は rows を空文字にする（"0"にフォールバックしない）。
    # "0"は「クエリは成功し、実際に0件だった」ことを意味する値として
    # 以降のロジック（92-98行目）が使うため、「クエリ自体が失敗した」
    # ケースと区別できないと、ClickHouse一時断が「バックアップ成功・
    # 0件」として誤記録され、5.1のprune安全条件（rclone check整合性
    # 確認済み）をすり抜けて未バックアップの実データが7日後に削除
    # されうる。
    local rows
    if ! rows=$(curl -sf --connect-timeout 5 --max-time 60 "${CH_AUTH_ARGS[@]}" "${CLICKHOUSE_URL}/" \
        --data "SELECT count() FROM ${table} WHERE toDate(${date_col}) = '${bdate}'" \
        2>/dev/null | tr -d '[:space:]'); then
        rows=""
    fi

    if curl -sf --connect-timeout 5 --max-time 300 "${CH_AUTH_ARGS[@]}" "${CLICKHOUSE_URL}/" \
        --data "SELECT * FROM ${table} WHERE toDate(${date_col}) = '${bdate}' FORMAT Parquet" \
        -o "$out" 2>>"$LOG_FILE" && [[ -s "$out" ]]; then

        local size
        size=$(du -sh "$out" | awk '{print $1}')
        log "[${table}] エクスポート完了: ${size} / ${rows:-0} rows"

        if rclone copy "$out" "${REMOTE}:${BUCKET}/clickhouse/${table}/" \
            --log-level WARNING --log-file "$LOG_FILE" 2>&1; then
            # アップロード成功の報告だけでは部分アップロード・破損を見逃す
            # 可能性がある。threat_events はバックアップ確認後にローカル
            # 削除する対象（prune_verified_threat_events）でもあるため、
            # ここでハッシュ/サイズ突合による整合性確認を必須にする。
            if rclone check "$(dirname "$out")" "${REMOTE}:${BUCKET}/clickhouse/${table}/" \
                --include "$(basename "$out")" --one-way \
                --log-level WARNING --log-file "$LOG_FILE" 2>&1; then
                log "[${table}] S3-compatible object storage アップロード完了・整合性確認OK"
                record_result "$table" "success" "${rows:-0}" "" "$bdate"
                rm -f "$out"
                [[ "$stale" != "$out" ]] && rm -f "$stale"
            else
                log "[${table}] S3-compatible object storage アップロード後の整合性確認に失敗（次回リトライ用にローカル保持）"
                record_result "$table" "upload_failed" "${rows:-0}" "rclone check mismatch" "$bdate"
                FAILURES+=("${table}: アップロード後の整合性確認に失敗（次回リトライ）")
            fi
        else
            log "[${table}] S3-compatible object storage アップロード失敗（次回リトライ用にローカル保持）"
            record_result "$table" "upload_failed" "${rows:-0}" "rclone upload failed" "$bdate"
            FAILURES+=("${table}: S3-compatible object storageアップロード失敗（次回リトライ）")
        fi
    else
        log "[${table}] エクスポート失敗（ClickHouse 未応答またはデータなし）"
        rm -f "$out"
        if [[ -z "$rows" ]]; then
            # 件数取得クエリ自体も失敗＝ClickHouse未応答の可能性が高く、
            # 本当に0件だったのか判別できない。"success"にはせず、export_failedとして
            # 記録する。以後の日次実行で、success記録の無い日は catch_up_failed_backups が
            # 再バックアップする（rowsが空でない"0"のときのみ、真の0件としてsuccess扱いする）。
            log "[${table}] 件数取得も失敗（ClickHouse未応答の可能性）。真の0件と区別できないため export_failed として記録"
            record_result "$table" "export_failed" "0" "ClickHouse unreachable (count query also failed)" "$bdate"
            FAILURES+=("${table}: ClickHouseエクスポート失敗（件数取得も失敗）")
        elif [[ "$rows" == "0" ]]; then
            log "[${table}] ${bdate}のデータなし → skip"
            record_result "$table" "success" "0" "" "$bdate"
        else
            record_result "$table" "export_failed" "0" "ClickHouse export error" "$bdate"
            FAILURES+=("${table}: ClickHouseエクスポート失敗")
        fi
    fi
}

# threat_events は PARTITION BY toYYYYMM・TTL 180日のため、90日の観測
# 期間中はTTLによる自動削除が一切効かず、ローカルディスクを圧迫し続ける
# （2026-07-13、成長率の実測から約40日で再逼迫すると判明）。S3(S3-compatible object storage)
# は1TBあり運用者の指示で全期間分を余裕を持って保持してよいため、
# バックアップ・整合性確認済みの日付から DELETE ミューテーション
# （DROP PARTITIONは月単位のため使えない）で日単位にローカル削除する。
# 安全のため以下をすべて満たす日付のみを対象にする：
#   - backup_log に table_name='threat_events', status='success' の記録がある
#     （rclone check による整合性確認まで通った日のみ「success」になる）
#   - まだ threat_events_local_prune として削除済み記録がない
#   - 7日以上前（後から問題が見つかっても対応できる猶予を残す）
#   - ローカルの現在件数が、バックアップ時に記録した件数（backup_log.row_count）を
#     超えていない（2026-09-24追加、doc/known-limitations.md #ZZ）。日次バックアップは
#     UTC日が終わった直後に走るため、日末の数秒分の行がバックアップ後に挿入されうる。
#     rclone checkは「アップロードしたファイルの完全性」しか保証せず、この後着行が
#     バックアップに含まれることまでは保証しない。超えている場合はその日を再エクスポート
#     して件数を揃え、揃った場合のみ削除する（揃わなければ削除せずFAILURESに積む）。
# どれか一つでも確認できなければ、その日のデータは削除せずローカルに
# 残す（ディスク逼迫より研究データの欠損の方が致命的なため）。

# threat_events の指定日の現在のローカル件数。取得できなければ空文字。
count_threat_events_on() {
    local d="$1"
    curl -sf --connect-timeout 5 --max-time 30 "${CH_AUTH_ARGS[@]}" "${CLICKHOUSE_URL}/" \
        --data "SELECT count() FROM threat_events WHERE toDate(timestamp) = '${d}'" \
        2>/dev/null | tr -d '[:space:]'
}

# backup_log上の「最新のsuccess」バックアップ件数（記録が無ければ0、取得できなければ空文字）。
latest_backed_rows() {
    local d="$1"
    curl -sf --connect-timeout 5 --max-time 30 "${CH_AUTH_ARGS[@]}" "${CLICKHOUSE_URL}/" \
        --data "SELECT argMax(row_count, created_at) FROM backup_log WHERE table_name = 'threat_events' AND status = 'success' AND backup_date = '${d}'" \
        2>/dev/null | tr -d '[:space:]'
}

prune_verified_threat_events() {
    local candidates
    candidates=$(curl -sf --connect-timeout 5 --max-time 30 "${CH_AUTH_ARGS[@]}" "${CLICKHOUSE_URL}/" --data "
        SELECT toString(bl.backup_date), argMax(bl.row_count, bl.created_at)
        FROM backup_log AS bl
        WHERE bl.table_name = 'threat_events' AND bl.status = 'success'
          AND bl.backup_date <= today() - 7
          AND bl.backup_date NOT IN (
              SELECT backup_date FROM backup_log
              WHERE table_name = 'threat_events_local_prune' AND status = 'success'
          )
        GROUP BY bl.backup_date
        ORDER BY bl.backup_date
    " 2>/dev/null) || candidates=""

    local d backed_rows rows_before
    while IFS=$'\t' read -r d backed_rows; do
        [[ -z "$d" ]] && continue
        rows_before=$(count_threat_events_on "$d")
        if [[ ! "$rows_before" =~ ^[0-9]+$ ]]; then
            log "[PRUNE] threat_events ${d}: ローカル件数を取得できないため削除しない"
            continue
        fi
        if [[ "$rows_before" == "0" ]]; then
            continue
        fi
        [[ "${backed_rows:-}" =~ ^[0-9]+$ ]] || backed_rows=0
        if [[ "$rows_before" -gt "$backed_rows" ]]; then
            log "[PRUNE] threat_events ${d}: ローカル${rows_before}行 > バックアップ記録${backed_rows}行（バックアップ後の後着行）。再エクスポートして件数を揃える"
            backup_table "threat_events" "timestamp" "$d"
            backed_rows=$(latest_backed_rows "$d")
            rows_before=$(count_threat_events_on "$d")
            if [[ ! "$backed_rows" =~ ^[0-9]+$ || ! "$rows_before" =~ ^[0-9]+$ \
                || "$rows_before" -gt "$backed_rows" ]]; then
                log "[PRUNE] threat_events ${d}: 再エクスポート後も件数が一致しない（ローカル${rows_before:-?}行 / バックアップ${backed_rows:-?}行）。削除せずローカルに保持"
                FAILURES+=("threat_events ${d}: ローカル件数がバックアップ記録件数を超えたまま（後着行がバックアップ未反映）。ローカル削除を見送り")
                continue
            fi
        fi
        if curl -sf --connect-timeout 5 --max-time 120 "${CH_AUTH_ARGS[@]}" "${CLICKHOUSE_URL}/" \
            --data "ALTER TABLE threat_events DELETE WHERE toDate(timestamp) = '${d}'" \
            >/dev/null 2>>"$LOG_FILE"; then
            log "[PRUNE] threat_events ${d} をローカル削除（バックアップ整合性確認済み、対象${rows_before}行、非同期ミューテーションのため反映に時間がかかる場合あり）"
            if ! python3 "$SCRIPT_DIR/record_backup_result.py" \
                --table "threat_events_local_prune" --status "success" --rows "${rows_before}" \
                --backup-date "${d}" --node-id "${NODE_ID}" --clickhouse-url "${CLICKHOUSE_URL}"; then
                # 削除自体は完了済みだが監査ログへの記録に失敗した。この日付は
                # 次回以降 rows_before=0 で continue するため自動再記録されない
                # （doc/known-limitations.md #FF）、通知して手動追記を促す。
                FAILURES+=("threat_events ${d}: ローカル削除は完了したがthreat_events_local_prune記録に失敗（doc/known-limitations.md #FF）")
            fi
        else
            log "[PRUNE] threat_events ${d} の削除失敗"
            FAILURES+=("threat_events ${d}: 検証済みデータのローカル削除に失敗")
        fi
    done <<< "$candidates"
}

# 直近14日（2〜14日前）でbackup_log上の最新ステータスがsuccessでない日（export_failed・
# upload_failed・記録なし）を、その日付で再バックアップする（1表あたり最大7日/回）。
# 以前は「export_failedは次回リトライ対象」とコメントされていたが、リトライ処理は
# ローカルに残った未アップロードParquetにしか働かず、export_failedの日は再試行されな
# かった。また残留Parquetのリトライ成功はbackup_logを更新しないため、その日は
# upload_failedのまま永久にpruneされなかった（doc/known-limitations.md #ZZ）。
# ClickHouseが応答しない場合はsuccess記録の有無を判定できないため、何もしない。
# 手動で日付を指定した実行（バックフィル等）では走らせない。
catch_up_failed_backups() {
    [[ -n "${_BACKUP_DATE_FROM_ENV:-}" ]] && return 0
    local spec table col ok_dates i d attempted
    for spec in "threat_events:timestamp" "pipeline_health:checked_at" "heartbeats:timestamp"; do
        table="${spec%%:*}"
        col="${spec##*:}"
        ok_dates=$(curl -sf --connect-timeout 5 --max-time 30 "${CH_AUTH_ARGS[@]}" "${CLICKHOUSE_URL}/" \
            --data "SELECT toString(backup_date) FROM (SELECT backup_date, argMax(status, created_at) AS st FROM backup_log WHERE table_name = '${table}' AND backup_date >= today() - 14 GROUP BY backup_date) WHERE st = 'success'" \
            2>/dev/null) || continue
        attempted=0
        for i in $(seq 2 14); do
            d=$(date -d "-${i} days" '+%Y-%m-%d')
            grep -qx "$d" <<< "$ok_dates" && continue
            [[ "$attempted" -ge 7 ]] && break
            log "[CATCH-UP] ${table} ${d}: 直近14日でsuccess記録が無いため再バックアップ"
            backup_table "$table" "$col" "$d"
            attempted=$((attempted + 1))
        done
    done
}

log "=== ClickHouse 日次バックアップ開始: ${BACKUP_DATE} ==="
log "ディスク使用率: $(disk_usage_summary)"

backup_table "threat_events"  "timestamp"
backup_table "pipeline_health" "checked_at"
# heartbeats は自己証明型完全性保証（研究の核心）の直接的な証拠データであり、
# データ量は他テーブルに比べ極小のため日次バックアップ対象に含める。
backup_table "heartbeats" "timestamp"

catch_up_failed_backups
prune_verified_threat_events

# 未アップロードの残留 Parquet をリトライ（前日以前の失敗分）
# 試行のたびには通知しない（開始・失敗時のみ・終了の3通に集約するため、
# 個々のリトライ結果は残留件数として最後のサマリにまとめる）
RETRY_REMAINING=0
for f in /tmp/obs-ch-backup-*/*.parquet; do
    [[ -f "$f" ]] || continue
    fname=$(basename "$f")
    tbl=$(echo "$fname" | sed 's/_[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\.parquet$//')
    log "[RETRY] ${fname}"
    if rclone copy "$f" "${REMOTE}:${BUCKET}/clickhouse/${tbl}/" \
        --log-level WARNING --log-file "$LOG_FILE" 2>&1; then
        rm -f "$f"
        log "[RETRY] 成功: ${fname}"
    else
        RETRY_REMAINING=$((RETRY_REMAINING + 1))
    fi
done
if [[ $RETRY_REMAINING -gt 0 ]]; then
    FAILURES+=("残留Parquet ${RETRY_REMAINING}件: リトライ後も未アップロード")
fi

rmdir "$BACKUP_TMP" 2>/dev/null || true
log "ディスク使用率: $(disk_usage_summary)"
log "=== バックアップ完了 ==="

if [[ ${#FAILURES[@]} -gt 0 ]]; then
    log "[OBS FAIL] ClickHouse日次バックアップ失敗: ${BACKUP_DATE}"
    log "失敗内容:"
    printf '  - %s\n' "${FAILURES[@]}" | while IFS= read -r line; do log "$line"; done
fi

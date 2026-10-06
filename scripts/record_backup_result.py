# self-proving-observation/
# └── scripts/
#     └── record_backup_result.py  — backup_log テーブルへの記録（バックアップスクリプト共通）
#
# backup_clickhouse.sh / rotate_and_backup.sh がそれぞれ個別に持っていた
# 同一のヘッドドキュメント（urllib で backup_log に INSERT する処理）を統合。

import argparse
import os
import sys
import urllib.parse
import urllib.request

_QUERY = (
    "INSERT INTO backup_log "
    "(backup_date, node_id, table_name, status, row_count, error_message) "
    "VALUES ({d:Date}, {n:String}, {t:String}, {s:String}, {r:UInt64}, {e:String})"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="backup_log テーブルへ成否を記録する")
    parser.add_argument("--table", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--rows", type=int, default=0)
    parser.add_argument("--error", default="")
    parser.add_argument("--backup-date", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--clickhouse-url", default="")
    parser.add_argument(
        "--quiet", action="store_true",
        help="URL 未設定・送信失敗時に何も出力せず終了する（rotate_and_backup.sh 用）",
    )
    args = parser.parse_args()

    if not args.clickhouse_url:
        if args.quiet:
            return
        args.clickhouse_url = "http://localhost:8123"

    ok = _record(args)
    if not ok:
        sys.exit(1)


def _record(args: argparse.Namespace) -> bool:
    """backup_log への INSERT を試みる。成否を bool で返す。

    doc/known-limitations.md #FF: 従来はここで例外を握りつぶした後
    main() が正常終了扱い（exit 0）で戻っていたため、backup_log 自体への
    記録失敗（＝自己証明型完全性保証の台帳そのものの欠落）を呼び出し元
    （backup_clickhouse.sh / rotate_and_backup.sh）が検知する手段が
    exit code 経由では存在しなかった。ここで真偽を返すことで、
    呼び出し元がFAILURES配列に積んで通知できるようにする。
    """

    params = urllib.parse.urlencode({
        "param_d": args.backup_date,
        "param_n": args.node_id,
        "param_t": args.table,
        "param_s": args.status,
        "param_r": args.rows,
        "param_e": args.error,
    })
    headers = {}
    ch_user = os.environ.get("CLICKHOUSE_USER")
    if ch_user:
        headers = {
            "X-ClickHouse-User": ch_user,
            "X-ClickHouse-Key": os.environ.get("CLICKHOUSE_PASSWORD", ""),
        }
    req = urllib.request.Request(  # nosec B310
        f"{args.clickhouse_url}/?{params}", data=_QUERY.encode(), headers=headers,
    )
    try:
        urllib.request.urlopen(req, timeout=10)  # nosec B310
    except Exception as ex:  # noqa: BLE001
        if not args.quiet:
            print(f"[WARN] backup_log 記録失敗: {ex}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    main()

# self-proving-observation/
# └── src/
#     └── main.py  — Threat event monitoring CLI

import argparse
import os
import sys
from pathlib import Path
from typing import Any

# `python3 src/main.py` のように直接パス実行された場合、プロジェクトルートが
# sys.path に乗らず `from src...` の import が失敗する。bash スクリプト側は
# 一貫してこの直接パス実行パターンを使うため、ここで明示的に補う。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.clickhouse import CLICKHOUSE_ERRORS, query_json
from src.logging_config import ensure_utf8_stdio

ensure_utf8_stdio()

CLICKHOUSE_ENDPOINT = os.environ.get("CLICKHOUSE_URL", "http://localhost:8123")


def query_clickhouse(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """ClickHouse HTTP API にパラメタライズドクエリを実行する。"""
    try:
        return query_json(CLICKHOUSE_ENDPOINT, sql, params, timeout=10)
    except CLICKHOUSE_ERRORS as e:
        print(f"[ERROR] ClickHouse 接続失敗 ({CLICKHOUSE_ENDPOINT}): {e}", file=sys.stderr)
        sys.exit(1)


def cmd_recent(args: argparse.Namespace) -> None:
    """直近の脅威イベントを表示する。"""
    rows = query_clickhouse(
        "SELECT timestamp, sensor_id, event_type, severity, count "
        "FROM threat_events "
        "WHERE timestamp >= now() - toIntervalHour({hours:UInt32}) "
        "ORDER BY timestamp DESC "
        "LIMIT {limit:UInt32}",
        params={"hours": args.hours, "limit": args.limit},
    )
    if not rows:
        print("該当イベントなし")
        return
    for row in rows:
        print(
            f"{row['timestamp']}  sensor={row['sensor_id']}  "
            f"type={row['event_type']}  severity={row['severity']}  "
            f"count={row['count']}"
        )


def cmd_summary(args: argparse.Namespace) -> None:
    """イベント種別ごとの集計サマリーを表示する。"""
    rows = query_clickhouse(
        "SELECT event_type, count() AS total, max(severity) AS max_severity "
        "FROM threat_events "
        "WHERE timestamp >= now() - toIntervalHour({hours:UInt32}) "
        "GROUP BY event_type "
        "ORDER BY total DESC",
        params={"hours": args.hours},
    )
    if not rows:
        print("該当イベントなし")
        return
    print(f"{'event_type':<30} {'total':>10} {'max_severity':>12}")
    print("-" * 54)
    for row in rows:
        print(
            f"{row['event_type']:<30} {row['total']:>10} "
            f"{row['max_severity']:>12}"
        )


def cmd_top_sensors(args: argparse.Namespace) -> None:
    """検知件数上位のセンサーを表示する。"""
    rows = query_clickhouse(
        "SELECT sensor_id, sum(count) AS total "
        "FROM threat_events "
        "WHERE timestamp >= now() - toIntervalHour({hours:UInt32}) "
        "GROUP BY sensor_id "
        "ORDER BY total DESC "
        "LIMIT {limit:UInt32}",
        params={"hours": args.hours, "limit": args.limit},
    )
    if not rows:
        print("該当イベントなし")
        return
    print(f"{'sensor_id':<40} {'total':>10}")
    print("-" * 52)
    for row in rows:
        print(f"{row['sensor_id']:<40} {row['total']:>10}")


def cmd_health(_args: argparse.Namespace) -> None:
    """パイプラインの正常性を確認する。"""
    checks: list[tuple[str, Any, bool]] = []

    rows = query_clickhouse(
        "SELECT count() AS cnt FROM threat_events "
        "WHERE timestamp >= now() - toIntervalMinute({minutes:UInt32})",
        params={"minutes": 5},
    )
    event_count = int(rows[0]["cnt"]) if rows else 0
    checks.append(("直近5分のイベント数", event_count, event_count > 0))

    rows = query_clickhouse(
        "SELECT uniqExact(sensor_id) AS cnt FROM threat_events "
        "WHERE timestamp >= now() - toIntervalHour(1)",
    )
    sensor_count = int(rows[0]["cnt"]) if rows else 0
    checks.append(("稼働中センサー数（1時間）", sensor_count, sensor_count > 0))

    rows = query_clickhouse(
        "SELECT max(timestamp) AS latest FROM threat_events",
    )
    latest = rows[0]["latest"] if rows and rows[0]["latest"] else "N/A"
    checks.append(("最新イベント時刻", latest, latest != "N/A"))

    print("=== Pipeline Health Check ===")
    all_ok = True
    for label, value, ok in checks:
        status = "OK" if ok else "NG"
        if not ok:
            all_ok = False
        print(f"  [{status}] {label}: {value}")

    if not all_ok:
        print("\n[WARN] 一部チェックが NG です。パイプラインの状態を確認してください。")
        sys.exit(1)
    print("\n[OK] パイプライン正常稼働中")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="脅威イベント監視 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_recent = sub.add_parser("recent", help="直近のイベント一覧")
    p_recent.add_argument("--hours", type=int, default=1)
    p_recent.add_argument("--limit", type=int, default=100)
    p_recent.set_defaults(func=cmd_recent)

    p_summary = sub.add_parser("summary", help="イベント種別サマリー")
    p_summary.add_argument("--hours", type=int, default=24)
    p_summary.set_defaults(func=cmd_summary)

    p_top = sub.add_parser("top-sensors", help="検知件数上位センサー")
    p_top.add_argument("--hours", type=int, default=24)
    p_top.add_argument("--limit", type=int, default=10)
    p_top.set_defaults(func=cmd_top_sensors)

    p_health = sub.add_parser("health", help="パイプライン正常性チェック")
    p_health.set_defaults(func=cmd_health)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

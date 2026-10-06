# self-proving-observation/
# └── scripts/
#     └── record_pipeline_health.py  — pipeline_health テーブルへの毎時集計記録
#
# 直近1時間の threat_events から件数・ユニークセンサー数を集計し、
# あわせて threat_events_experiment の現在の行数・バイト数（system.parts の
# active パーツ合計）も記録して、pipeline_health に1行追加する。後者は
# TTL失効前のテーブル肥大化がClickHouse書き込み速度を低下させた事故を受けて追加した推移監視用の列（即時
# アラートを担っていたscripts/check_experiment_table_growth.shは通知系
# 撤去に伴い削除済み。この推移記録自体は維持）。
# ClickHouse 側で完結する INSERT...SELECT のため呼び出し元は ClickHouse URL
# のみ渡せばよい。
#
# Cron（proposed-node）: 0 * * * * python3 scripts/record_pipeline_health.py

import argparse
import sys
from pathlib import Path

# プロジェクトルートが sys.path に乗らず `from src...` の import が失敗する。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.clickhouse import CLICKHOUSE_ERRORS, clickhouse_request  # noqa: E402

_QUERY = (
    "INSERT INTO pipeline_health "
    "(checked_at, events_last_hour, unique_sensors, experiment_table_rows, experiment_table_bytes) "
    "SELECT now(), count(), uniqExact(sensor_id), "
    "(SELECT sum(rows) FROM system.parts "
    "WHERE table = 'threat_events_experiment' AND active), "
    "(SELECT sum(bytes_on_disk) FROM system.parts "
    "WHERE table = 'threat_events_experiment' AND active) "
    "FROM threat_events WHERE timestamp >= now() - INTERVAL 1 HOUR"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="pipeline_health テーブルへ毎時集計を記録する")
    parser.add_argument("--clickhouse-url", default="http://localhost:8123")
    args = parser.parse_args()

    url = f"{args.clickhouse_url}/"
    try:
        clickhouse_request(url, _QUERY.encode(), timeout=30)
    except CLICKHOUSE_ERRORS as ex:
        print(f"[WARN] pipeline_health 記録失敗: {ex}", file=sys.stderr)


if __name__ == "__main__":
    main()

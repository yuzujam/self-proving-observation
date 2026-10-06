# self-proving-observation/
# └── src/
#     └── measure/
#         └── loss_rate.py  — 欠損率計測（inject_id 突合）

import argparse
import csv
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

# `python3 src/measure/loss_rate.py` のように直接パス実行された場合、
# プロジェクトルートが sys.path に乗らず `from src...` の import が失敗する。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.clickhouse import CLICKHOUSE_ERRORS, clickhouse_request
from src.logging_config import ensure_utf8_stdio
from src.measure._common import ensure_parent_dir

ensure_utf8_stdio()

# ES検証クエリの429（Too Many Requests）に対するリトライ回数・初期待機秒数。
# 429は一過性のレート制限であり恒久的な障害ではないため、即座に
# verification_failed扱いにせず指数バックオフで再試行する
# （known-limitations.md #X、対照実験のbaseline検証失敗率が高い問題への対応）。
ES_RETRY_MAX_ATTEMPTS = 4
ES_RETRY_BASE_DELAY_SECONDS = 2.0


def load_inject_log(csv_path: str) -> tuple[dict[str, set[str]], str | None, str | None]:
    """送信ログ CSV を読み込み、ターゲットごとの inject_id 集合と時間範囲を返す。

    sent_ok=0 の行（送信失敗）は除外する。旧フォーマット（sent_ok列なし）は全行を含む。
    Returns: (target_ids, ts_min, ts_max)
    """
    target_ids: dict[str, set[str]] = {}
    timestamps: list[str] = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("sent_ok", "1") != "1":
                continue
            target = row["target"]
            inject_id = row["inject_id"]
            if target not in target_ids:
                target_ids[target] = set()
            target_ids[target].add(inject_id)
            if row.get("timestamp"):
                timestamps.append(row["timestamp"])

    ts_min = min(timestamps) if timestamps else None
    ts_max = max(timestamps) if timestamps else None
    return target_ids, ts_min, ts_max


def _utc_naive(iso: str) -> datetime:
    """ISO8601文字列をUTCのタイムゾーン情報なしdatetimeへ変換する（ClickHouseセッションTZはUTC）。

    オフセット付きの値はオフセットを捨てるのではなくUTCへ変換する。オフセットなしは
    UTCとみなす（doc/known-limitations.md #VV、worker.pyの`_fmt_ts`と同じ扱い）。
    """
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.replace(tzinfo=None)


def query_clickhouse_ids(
    endpoint: str,
    ts_from: str | None = None,
    ts_to: str | None = None,
    query_failed: list[Any] | None = None,
) -> set[str]:
    """ClickHouse から記録済み inject_id を取得する（時間範囲フィルタ・TSV形式）。

    query_failed: 渡された場合、クエリ自体が失敗した際に True を追記する。
    「クエリ失敗で0件」と「クエリ成功で0件」を呼び出し側が区別するためのフラグで、
    未指定時は従来通りの動作（戻り値は空集合のみ）を維持する。
    """
    parsed = urlparse(endpoint)
    user = parsed.username or ""
    password = parsed.password or ""
    if user or password:
        netloc = f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or "")
        endpoint = urlunparse(parsed._replace(netloc=netloc))

    # 実験時間帯に絞ることで複数回実験が蓄積したテーブルでも高速動作する
    where_clauses = ["inject_id != ''"]
    url_params: dict[str, str] = {}

    if ts_from:
        ts_from_dt = _utc_naive(ts_from)
        url_params["param_ts_from"] = ts_from_dt.strftime("%Y-%m-%d %H:%M:%S")
        where_clauses.append("timestamp >= {ts_from:DateTime64(3)}")
    if ts_to:
        # +10分バッファ: Redis伝搬遅延を吸収（キュードレイン後に呼ばれるが念のため）
        ts_to_dt = _utc_naive(ts_to) + timedelta(minutes=10)
        url_params["param_ts_to"] = ts_to_dt.strftime("%Y-%m-%d %H:%M:%S")
        where_clauses.append("timestamp <= {ts_to:DateTime64(3)}")

    # 合成注入トラフィックはworker.pyによりthreat_events_experiment（短いTTL）へ
    # 振り分けられる（doc/known-limitations.md #L）。本体のthreat_eventsは
    # 90日観測データ専用のため、対照実験の突合はこちらを参照する。
    where = " AND ".join(where_clauses)
    sql = (
        f"SELECT DISTINCT inject_id FROM threat_events_experiment "  # noqa: S608  # nosec
        f"WHERE {where} FORMAT TabSeparated"
    )

    headers: dict[str, str] = {}
    if user:
        headers["X-ClickHouse-User"] = user
    if password:
        headers["X-ClickHouse-Key"] = password

    url = f"{endpoint}?{urlencode(url_params)}" if url_params else endpoint
    try:
        content = clickhouse_request(
            url, sql.encode("utf-8"), headers=headers, timeout=120,
        ).decode("utf-8")
        return {line.strip() for line in content.splitlines() if line.strip()}
    except CLICKHOUSE_ERRORS as e:
        print(f"[ERROR] ClickHouse query failed: {e}", file=sys.stderr)
        if query_failed is not None:
            query_failed.append(True)
        return set()


def query_elasticsearch_ids(
    endpoint: str,
    ts_from: str | None = None,
    ts_to: str | None = None,
    query_failed: list[Any] | None = None,
) -> set[str]:
    """Elasticsearch から記録済み inject_id を取得する（複合集約ページネーション対応）。

    terms aggregation は default で 65,536 バケット上限があるため composite aggregation
    で 10,000 件ずつページングし、任意件数の inject_id に対応する。

    query_failed: 渡された場合、クエリ自体が失敗した際に True を追記する（ページ途中の
    失敗を含む）。「クエリ失敗で0件」と「クエリ成功で0件」を呼び出し側が区別するための
    フラグで、未指定時は従来通りの動作（戻り値はそれまでに集めた集合のみ）を維持する。
    """
    all_ids: set[str] = set()
    after_key: dict[str, Any] | None = None

    time_filter: list[dict[str, Any]] = []
    if ts_from or ts_to:
        range_clause: dict[str, Any] = {}
        if ts_from:
            range_clause["gte"] = ts_from
        if ts_to:
            ts_to_buf = (
                datetime.fromisoformat(ts_to) + timedelta(minutes=10)
            ).isoformat()
            range_clause["lte"] = ts_to_buf
        time_filter.append({"range": {"@timestamp": range_clause}})

    while True:
        composite: dict[str, Any] = {
            "size": 10000,
            "sources": [{"inject_id": {"terms": {"field": "inject_id.keyword"}}}],
        }
        if after_key:
            composite["after"] = after_key

        query: dict[str, Any] = {
            "size": 0,
            "aggs": {"unique_ids": {"composite": composite}},
            "query": {
                "bool": {
                    "must_not": [{"term": {"inject_id.keyword": ""}}],
                    "filter": time_filter,
                }
            },
        }

        url = f"{endpoint}/threat-events-*/_search"
        body = json.dumps(query).encode("utf-8")

        result: dict[str, Any] = {}
        for attempt in range(ES_RETRY_MAX_ATTEMPTS):
            req = Request(  # nosec B310
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(req, timeout=60) as resp:  # nosec B310
                    result = json.loads(resp.read().decode("utf-8"))
                break
            except HTTPError as e:
                if e.code == 429 and attempt < ES_RETRY_MAX_ATTEMPTS - 1:
                    delay = ES_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
                    print(
                        f"[WARN] Elasticsearch 429 (Too Many Requests)、"
                        f"{delay:.0f}秒後にリトライ ({attempt + 1}/{ES_RETRY_MAX_ATTEMPTS})",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                print(f"[ERROR] Elasticsearch query failed: {e}", file=sys.stderr)
                if query_failed is not None:
                    query_failed.append(True)
                return all_ids
            except (URLError, HTTPException, OSError) as e:
                print(f"[ERROR] Elasticsearch query failed: {e}", file=sys.stderr)
                if query_failed is not None:
                    query_failed.append(True)
                return all_ids

        agg = result.get("aggregations", {}).get("unique_ids", {})
        buckets = agg.get("buckets", [])
        for b in buckets:
            all_ids.add(b["key"]["inject_id"])

        after_key = agg.get("after_key")
        if not after_key or len(buckets) < 10000:
            break

    return all_ids


def _host_of(url: str) -> str:
    """URL または netloc 文字列からホスト名を返す。

    urlparse は "localhost:8000" のようなホスト名形式の netloc を
    scheme="localhost", path="8000" と誤解釈するため、
    hostname が取れなかった場合は "//" を補完して再パースする。
    """
    parsed = urlparse(url)
    if parsed.hostname:
        return parsed.hostname
    reparsed = urlparse(f"//{url}")
    return reparsed.hostname or url.split(":")[0]


def measure_loss(
    inject_log_path: str,
    clickhouse_url: str | None = None,
    elasticsearch_url: str | None = None,
    output_path: str | None = None,
    proposed_target: str | None = None,
    baseline_target: str | None = None,
) -> dict[str, Any]:
    """欠損率を計測する。"""
    target_ids, ts_min, ts_max = load_inject_log(inject_log_path)
    results = {}

    if clickhouse_url:
        query_failed: list[Any] = []
        recorded = query_clickhouse_ids(clickhouse_url, ts_min, ts_max, query_failed)
        match_host = _host_of(proposed_target) if proposed_target else _host_of(clickhouse_url)
        for target, injected_ids in target_ids.items():
            if _host_of(target) == match_host:
                total = len(injected_ids)
                found = len(injected_ids & recorded)
                lost = total - found
                rate = (lost / total * 100) if total > 0 else 0.0
                results["proposed"] = {
                    "total_injected": total,
                    "total_recorded": found,
                    "total_lost": lost,
                    "loss_rate_percent": round(rate, 4),
                    "verification_failed": bool(query_failed),
                }

    if elasticsearch_url:
        query_failed = []
        recorded = query_elasticsearch_ids(elasticsearch_url, ts_min, ts_max, query_failed)
        match_host = _host_of(baseline_target) if baseline_target else _host_of(elasticsearch_url)
        for target, injected_ids in target_ids.items():
            if _host_of(target) == match_host:
                total = len(injected_ids)
                found = len(injected_ids & recorded)
                lost = total - found
                rate = (lost / total * 100) if total > 0 else 0.0
                results["baseline"] = {
                    "total_injected": total,
                    "total_recorded": found,
                    "total_lost": lost,
                    "loss_rate_percent": round(rate, 4),
                    "verification_failed": bool(query_failed),
                }

    print("=== Loss Rate Report ===")
    for node, data in results.items():
        if data.get("verification_failed"):
            status = "UNVERIFIED"
        elif data["loss_rate_percent"] == 0:
            status = "OK"
        else:
            status = "DATA LOSS"
        print(
            f"  [{status}] {node}: "
            f"injected={data['total_injected']}  "
            f"recorded={data['total_recorded']}  "
            f"lost={data['total_lost']}  "
            f"loss_rate={data['loss_rate_percent']}%"
            + ("  (verification query failed — not a confirmed loss)"
               if data.get("verification_failed") else "")
        )

    if output_path:
        ensure_parent_dir(output_path)
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\n[INFO] Report saved to {output_path}")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="欠損率計測",
    )
    parser.add_argument("--inject-log", required=True, help="送信ログ CSV パス")
    parser.add_argument("--clickhouse", default="http://localhost:8123", help="ClickHouse URL")
    parser.add_argument(
        "--elasticsearch", default="http://localhost:9200", help="Elasticsearch URL",
    )
    parser.add_argument("--output", default="results/loss_rate.json", help="結果出力パス")
    parser.add_argument(
        "--proposed-target", default=None,
        help="proposed ノードの送信先 URL（inject_log との突合に使用）",
    )
    parser.add_argument(
        "--baseline-target", default=None,
        help="baseline ノードの送信先 URL（inject_log との突合に使用）",
    )

    args = parser.parse_args()
    measure_loss(
        args.inject_log, args.clickhouse, args.elasticsearch, args.output,
        args.proposed_target, args.baseline_target,
    )


if __name__ == "__main__":
    main()

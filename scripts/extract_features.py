# self-proving-observation/
# └── scripts/
#     └── extract_features.py  — T-Pot ES インデックスを1分集計CSVに変換

import argparse
import csv
import json
import os
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

ES_URL = os.environ.get("ES_URL", "http://localhost:64298")

FEATURE_NAMES = [
    "window_start", "event_count", "unique_sensors", "avg_severity",
    "max_severity", "alert_count", "dns_count", "http_count",
    "tls_count", "flow_count", "ssh_count",
]


def es_request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{ES_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except URLError as e:
        print(f"[ERROR] ES request failed: {e}", file=sys.stderr)
        raise


def extract_features(index: str) -> list[dict]:
    """ES aggregation API で1分ウィンドウの特徴量を一括取得する（スクロール不要）。"""
    body = {
        "size": 0,
        "aggs": {
            "by_minute": {
                "date_histogram": {
                    "field": "@timestamp",
                    "fixed_interval": "1m",
                    "format": "yyyy-MM-dd HH:mm",
                    "min_doc_count": 1,
                },
                "aggs": {
                    "unique_sensors": {
                        "cardinality": {"field": "type.keyword"}
                    },
                    "avg_severity": {
                        "avg": {"field": "alert.severity"}
                    },
                    "max_severity": {
                        "max": {"field": "alert.severity"}
                    },
                    "alert_count": {
                        "filter": {"term": {"event_type.keyword": "alert"}}
                    },
                    "dns_count": {
                        "filter": {"term": {"event_type.keyword": "dns"}}
                    },
                    "http_count": {
                        "filter": {"term": {"event_type.keyword": "http"}}
                    },
                    "tls_count": {
                        "filter": {"term": {"event_type.keyword": "tls"}}
                    },
                    "flow_count": {
                        "filter": {"term": {"event_type.keyword": "flow"}}
                    },
                    "ssh_count": {
                        "filter": {
                            "bool": {
                                "should": [
                                    {"term": {"type.keyword": "Cowrie"}},
                                    {"term": {"dest_port": 22}},
                                ],
                                "minimum_should_match": 1,
                            }
                        }
                    },
                },
            }
        },
    }

    resp = es_request("POST", f"/{index}/_search", body)
    buckets = resp.get("aggregations", {}).get("by_minute", {}).get("buckets", [])

    rows = []
    for b in buckets:
        avg_sev = b["avg_severity"]["value"]
        max_sev = b["max_severity"]["value"]
        rows.append({
            "window_start": b["key_as_string"],
            "event_count": b["doc_count"],
            "unique_sensors": b["unique_sensors"]["value"],
            "avg_severity": round(avg_sev, 4) if avg_sev is not None else 0.0,
            "max_severity": round(max_sev, 4) if max_sev is not None else 0.0,
            "alert_count": b["alert_count"]["doc_count"],
            "dns_count": b["dns_count"]["doc_count"],
            "http_count": b["http_count"]["doc_count"],
            "tls_count": b["tls_count"]["doc_count"],
            "flow_count": b["flow_count"]["doc_count"],
            "ssh_count": b["ssh_count"]["doc_count"],
        })

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="T-Pot ES インデックスを1分集計CSVに変換する",
    )
    parser.add_argument("index", help="ES インデックス名 (例: logstash-2026.06.29)")
    parser.add_argument("--output", "-o", default="-",
                        help="出力CSVパス（デフォルト: stdout）")
    args = parser.parse_args()

    print(f"[EXTRACT] {args.index} を集計中...", file=sys.stderr)
    rows = extract_features(args.index)
    print(f"[EXTRACT] {len(rows)} 分のウィンドウを生成", file=sys.stderr)

    if not rows:
        print("[EXTRACT] データなし", file=sys.stderr)
        sys.exit(1)

    if args.output == "-":
        out = sys.stdout
        writer = csv.DictWriter(out, fieldnames=FEATURE_NAMES)
        writer.writeheader()
        writer.writerows(rows)
    else:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FEATURE_NAMES)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[EXTRACT] 保存: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()

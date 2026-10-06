# self-proving-observation/
# └── scripts/
#     └── measure_ablation_latency.py
#
# doc/pipeline-spec.md「補強実験: 集約ウィンドウのアブレーション実験」の一部。
# vector-ablation.tomlのfileシンク出力（集約後レコードのNDJSON）をテールし、
# 行ごとの壁時計到達時刻とvector_ingest_ts_first/_lastの差分から、集約ウィンドウが
# 追加する待機時間を実測する。あわせてcount合計を突合し、集約処理自体の
# イベント取りこぼしがないことを確認する（正確性チェック）。

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_config import get_logger

logger = get_logger(__name__)


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def _summarize(values: list[float]) -> dict:
    return {
        "mean": sum(values) / len(values) if values else None,
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def tail_and_measure(output_path: Path, duration_s: float, poll_interval: float) -> dict:
    """出力ファイルを`duration_s`秒間ポーリングし、新規行ごとに待機時間を算出する。

    - latency_upper_ms: ウィンドウ内で最も早く取り込まれたイベントが経験した待機時間（上限）
    - latency_lower_ms: ウィンドウ内で最も遅く取り込まれたイベントが経験した待機時間（下限）
    """
    latencies_upper: list[float] = []
    latencies_lower: list[float] = []
    total_count = 0
    n_groups = 0
    parse_errors = 0

    deadline = time.monotonic() + duration_s
    pos = 0
    while time.monotonic() < deadline:
        if output_path.exists():
            with open(output_path, encoding="utf-8") as f:
                f.seek(pos)
                new_lines = f.readlines()
                pos = f.tell()
            observed_at_ms = time.time() * 1000
            for raw_line in new_lines:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    first_ms = float(rec["vector_ingest_ts_first"])
                    last_ms = float(rec["vector_ingest_ts_last"])
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    parse_errors += 1
                    continue

                latencies_upper.append(observed_at_ms - first_ms)
                latencies_lower.append(observed_at_ms - last_ms)
                total_count += int(rec.get("count", 0))
                n_groups += 1
        time.sleep(poll_interval)

    return {
        "n_groups": n_groups,
        "total_count": total_count,
        "parse_errors": parse_errors,
        "latency_upper_ms": _summarize(latencies_upper),
        "latency_lower_ms": _summarize(latencies_lower),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="アブレーション実験: 集約出力をテールしレイテンシ・正確性を測定する",
    )
    parser.add_argument("--output-file", required=True, help="Vectorのfileシンク出力パス")
    parser.add_argument("--duration", type=float, required=True, help="計測を続ける秒数")
    parser.add_argument("--poll-interval", type=float, default=0.2, help="ポーリング間隔（秒）")
    parser.add_argument("--result-output", required=True, help="結果JSONの保存先")
    args = parser.parse_args()

    logger.info(f"[ABLATION-MEASURE] {args.output_file} を{args.duration}秒間テールします")
    result = tail_and_measure(Path(args.output_file), args.duration, args.poll_interval)
    with open(args.result_output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    logger.info(
        f"[ABLATION-MEASURE] 完了。n_groups={result['n_groups']}  "
        f"total_count={result['total_count']}  parse_errors={result['parse_errors']}  "
        f"保存先={args.result_output}"
    )


if __name__ == "__main__":
    main()

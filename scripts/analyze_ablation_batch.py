# self-proving-observation/
# └── scripts/
#     └── analyze_ablation_batch.py
#
# scripts/run_ablation_batch.sh が生成する results/ablation_batch_* ディレクトリ
# （複数のWINDOW_MS×複数試行を積み上げたもの）を集計し、doc/pipeline-spec.md
# 「補強実験: 集約ウィンドウのアブレーション実験」の目的1〜3（正確性・レイテンシ・
# オーバーヘッド）をウィンドウ長ごとに要約する。analyze_multiedge_results.py・
# pool_control_experiment_stats.pyと同種の、対話セッション向けの一時的な再解析
# スクリプトという位置づけ（結果ディレクトリの恒久的な分析基盤ではない）。

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_config import ensure_utf8_stdio

ensure_utf8_stdio()


def load_trial(trial_dir: Path) -> dict | None:
    """1つのwN_trialM/ディレクトリからresults.json・resource_vector.csvを読み込む。

    いずれか欠けている場合はNoneを返し、呼び出し元でスキップできるようにする
    （バッチ中断・異常終了の可能性を隠蔽しないため、件数として明示する）。
    """
    results_path = trial_dir / "results.json"
    resource_path = trial_dir / "resource_vector.csv"
    if not results_path.exists() or not resource_path.exists():
        return None

    with open(results_path, encoding="utf-8") as f:
        results = json.load(f)

    cpu_values: list[float] = []
    mem_percent_values: list[float] = []
    containers: set[str] = set()
    with open(resource_path, encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        idx = {name: i for i, name in enumerate(header)}
        for line in f:
            fields = line.rstrip("\n").split(",")
            if len(fields) <= max(idx.values()):
                continue
            containers.add(fields[idx["container"]])
            try:
                cpu_values.append(float(fields[idx["cpu_percent"]]))
                mem_percent_values.append(float(fields[idx["mem_percent"]]))
            except ValueError:
                continue

    return {
        "dir": str(trial_dir),
        "window_ms": results["window_ms"],
        "count_matches": results["count_matches"],
        "count_diff": results["count_diff"],
        "n_aggregated_groups": results["n_aggregated_groups"],
        "latency_upper_ms": results["latency_upper_ms"],
        "latency_lower_ms": results["latency_lower_ms"],
        "cpu_peak": max(cpu_values) if cpu_values else None,
        "cpu_mean": sum(cpu_values) / len(cpu_values) if cpu_values else None,
        "mem_percent_peak": max(mem_percent_values) if mem_percent_values else None,
        "containers_seen": sorted(containers),
        "n_resource_samples": len(cpu_values),
    }


def group_by_window(trials: list[dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for t in trials:
        grouped.setdefault(t["window_ms"], []).append(t)
    return grouped


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_window(window_ms: int, trials: list[dict]) -> dict:
    n = len(trials)
    n_correct = sum(1 for t in trials if t["count_matches"])
    unexpected_containers = sorted(
        {c for t in trials for c in t["containers_seen"] if c != "obs-ablation-vector"}
    )
    return {
        "window_ms": window_ms,
        "n_trials": n,
        "n_count_matches": n_correct,
        "correctness_rate": n_correct / n if n else None,
        "unexpected_containers": unexpected_containers,
        "latency_upper_ms_mean_of_means": _mean([t["latency_upper_ms"]["mean"] for t in trials]),
        "latency_upper_ms_mean_of_p95": _mean([t["latency_upper_ms"]["p95"] for t in trials]),
        "latency_upper_ms_max_of_max": max(
            (t["latency_upper_ms"]["max"] for t in trials), default=None
        ),
        "latency_lower_ms_mean_of_means": _mean([t["latency_lower_ms"]["mean"] for t in trials]),
        "latency_lower_ms_mean_of_p95": _mean([t["latency_lower_ms"]["p95"] for t in trials]),
        "latency_lower_ms_max_of_max": max(
            (t["latency_lower_ms"]["max"] for t in trials), default=None
        ),
        "cpu_peak_mean": _mean([t["cpu_peak"] for t in trials if t["cpu_peak"] is not None]),
        "cpu_peak_max": max(
            (t["cpu_peak"] for t in trials if t["cpu_peak"] is not None), default=None
        ),
        "mem_percent_peak_mean": _mean(
            [t["mem_percent_peak"] for t in trials if t["mem_percent_peak"] is not None]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="results/ablation_batch_*/wN_trialM を横断してウィンドウ長ごとに要約する",
    )
    parser.add_argument(
        "batch_dir",
        nargs="+",
        help="results/ablation_batch_* ディレクトリのパス（複数指定でtrialを合算）",
    )
    parser.add_argument("--output", default="", help="結果JSONの保存先（省略時は標準出力のみ）")
    args = parser.parse_args()

    batch_dirs = [Path(p) for p in args.batch_dir]
    trial_dirs = sorted(
        p for batch_dir in batch_dirs for p in batch_dir.iterdir() if p.is_dir()
    )

    trials = []
    skipped = 0
    for d in trial_dirs:
        trial = load_trial(d)
        if trial is None:
            skipped += 1
            continue
        trials.append(trial)

    print(f"[ABLATION-BATCH] {len(trials)}試行を読み込み（{skipped}件は不完全のためスキップ）")
    if not trials:
        print("[ABLATION-BATCH] 有効な試行が0件のため終了します。")
        return

    grouped = group_by_window(trials)
    summaries = [summarize_window(w, ts) for w, ts in sorted(grouped.items())]

    print("\n=== ウィンドウ長ごとの要約 ===")
    for s in summaries:
        print(f"\n  window_ms={s['window_ms']}  (n={s['n_trials']}試行)")
        print(
            f"    正確性: {s['n_count_matches']}/{s['n_trials']} 一致"
            f"  異常container: {s['unexpected_containers'] or 'なし'}"
        )
        print(
            f"    latency_upper_ms: mean(of means)={s['latency_upper_ms_mean_of_means']:.1f}"
            f"  mean(of p95)={s['latency_upper_ms_mean_of_p95']:.1f}"
            f"  max={s['latency_upper_ms_max_of_max']:.1f}"
        )
        print(
            f"    latency_lower_ms: mean(of means)={s['latency_lower_ms_mean_of_means']:.1f}"
            f"  mean(of p95)={s['latency_lower_ms_mean_of_p95']:.1f}"
            f"  max={s['latency_lower_ms_max_of_max']:.1f}"
        )
        cpu_peak_mean = s["cpu_peak_mean"]
        cpu_peak_max = s["cpu_peak_max"]
        mem_peak_mean = s["mem_percent_peak_mean"]
        if cpu_peak_mean is not None:
            print(f"    cpu_peak: mean={cpu_peak_mean:.2f}%  max={cpu_peak_max:.2f}%")
        else:
            print("    cpu_peak: N/A")
        if mem_peak_mean is not None:
            print(f"    mem_percent_peak(mean): {mem_peak_mean:.2f}%")
        else:
            print("    mem_percent_peak: N/A")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"trials": trials, "summaries": summaries}, f, indent=2, ensure_ascii=False)
        print(f"\n[ABLATION-BATCH] 結果を保存しました: {args.output}")


if __name__ == "__main__":
    main()

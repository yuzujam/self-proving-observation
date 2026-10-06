# self-proving-observation/
# └── src/
#     └── measure/
#         └── aggregate.py  — バッチ実験結果の統計集約

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np

# `python3 src/measure/aggregate.py` のように直接パス実行された場合、
# プロジェクトルートが sys.path に乗らず `from src...` の import が失敗する。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.logging_config import ensure_utf8_stdio
from src.measure._common import ensure_parent_dir, group_runs

ensure_utf8_stdio()


def parse_run_name(name: str) -> dict[str, Any] | None:
    """ディレクトリ名から条件を抽出する。"""
    match = re.match(r"(.+)_rps(\d+)_trial(\d+)$", name)
    if not match:
        return None
    return {
        "pattern": match.group(1),
        "rps": int(match.group(2)),
        "trial": int(match.group(3)),
    }


def parse_resource_csv(csv_path: str, container_prefix: str = "") -> dict[str, Any]:
    """リソース CSV からピーク値・平均値を算出する。

    `monitor/resource.py`の`collect_docker_stats_ssh`はSSH先ホスト上で
    稼働中の全コンテナを無差別に記録するため（対象compose projectで
    フィルタしない）、resource_baseline.csv／resource_proposed.csvには
    同居する無関係なコンテナ（本番T-Potハニーポット群・逆側のスタック等）
    が混入しうる（doc/known-limitations.md参照）。`container_prefix`を
    指定すると、`container`列がその接頭辞で始まる行のみに絞り込む。
    未指定時（既定値""）は従来通り全行を対象とし後方互換を保つ。
    """
    cpu_values = []
    mem_values = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if container_prefix and not row.get("container", "").startswith(container_prefix):
                continue
            try:
                cpu_values.append(float(row["cpu_percent"]))
            except (ValueError, KeyError):
                pass
            try:
                mem_values.append(float(row["mem_percent"]))
            except (ValueError, KeyError):
                pass

    if not cpu_values or not mem_values:
        return {}

    return {
        "cpu_peak": round(max(cpu_values), 2),
        "cpu_mean": round(sum(cpu_values) / len(cpu_values), 2),
        "mem_peak": round(max(mem_values), 2),
        "mem_mean": round(sum(mem_values) / len(mem_values), 2),
    }


def _parse_mem_mib(mem_usage: str) -> float | None:
    """`docker stats`の`MemUsage`列（例: "665.4MiB / 1GiB"）の使用量側をMiB単位へ変換する。

    パース不能な場合はNoneを返す（呼び出し元でスキップし、他行の値で穴埋めしない）。
    """
    used = mem_usage.split("/")[0].strip()
    match = re.match(r"^([\d.]+)\s*(B|KiB|MiB|GiB|TiB)$", used)
    if not match:
        return None
    scale = {
        "B": 1.0 / (1024 ** 2), "KiB": 1.0 / 1024, "MiB": 1.0,
        "GiB": 1024.0, "TiB": 1024.0 ** 2,
    }[match.group(2)]
    return float(match.group(1)) * scale


def aggregate_resource_totals(
    csv_paths: list[str], container_prefix: str,
) -> dict[str, Any]:
    """複数のリソースCSVから「時刻ごとの全対象コンテナ合計」の系列をプールし統計を出す。

    `doc/paper-draft.md` 6.5節の表が示す指標（時刻ごとに対象スタックの全コンテナの
    値を合計してから、その合計値の系列全体に対して平均・中央値・p95を取る）を
    再現する。`parse_resource_csv()`が返す行単位のpeak/mean（コンテナも時刻も
    区別せず1本の系列として扱う）とは異なる指標のため、独立した関数として実装する
    （`doc/known-limitations.md` #GGG: 従来この指標を算出した手順がリポジトリに
    存在せず対話セッションでの個別集計に依存していた）。

    `csv_paths`は複数の`resource_baseline.csv`/`resource_proposed.csv`（複数バッチ・
    複数試行分）を渡せる。ファイルをまたいで同一時刻ラベルが偶然一致しても、
    ファイルごとに独立した1標本として扱う（キーに`csv_path`を含めるため合算されない）。
    """
    cpu_by_key: dict[str, float] = {}
    mem_by_key: dict[str, float] = {}

    for csv_path in csv_paths:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                container = row.get("container", "")
                if not container.startswith(container_prefix):
                    continue
                key = f"{csv_path}:{row.get('timestamp', '')}"
                try:
                    cpu_by_key[key] = cpu_by_key.get(key, 0.0) + float(row["cpu_percent"])
                except (ValueError, KeyError):
                    pass
                mem_mib = _parse_mem_mib(row.get("mem_usage", ""))
                if mem_mib is not None:
                    mem_by_key[key] = mem_by_key.get(key, 0.0) + mem_mib

    cpu_totals = list(cpu_by_key.values())
    mem_totals = list(mem_by_key.values())

    def _summary(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"mean": None, "median": None, "p95": None}
        arr = np.array(values)
        return {
            "mean": round(float(arr.mean()), 1),
            "median": round(float(np.median(arr)), 1),
            "p95": round(float(np.percentile(arr, 95)), 1),
        }

    return {
        "n_timestamps": len(cpu_totals),
        "cpu_percent_total": _summary(cpu_totals),
        "mem_mib_total": _summary(mem_totals),
    }


def scan_batch_dir(batch_dir: str) -> list[dict[str, Any]]:
    """バッチディレクトリを走査し全試行の結果を収集する。"""
    runs = []
    for entry in sorted(os.listdir(batch_dir)):
        run_dir = os.path.join(batch_dir, entry)
        if not os.path.isdir(run_dir):
            continue

        condition = parse_run_name(entry)
        if condition is None:
            continue

        loss_file = os.path.join(run_dir, "loss_rate.json")
        loss_data = {}
        if os.path.exists(loss_file):
            with open(loss_file) as f:
                loss_data = json.load(f)

        resources = {}
        for system in ["baseline", "proposed"]:
            csv_path = os.path.join(run_dir, f"resource_{system}.csv")
            if os.path.exists(csv_path):
                # 対象スタックのコンテナ（compose project名 baseline / proposed が
                # コンテナ名の接頭辞）のみに絞る。SSH経由のdocker statsは同居する
                # 本番T-Pot群や逆側スタックも記録するため（doc/known-limitations.md
                # #AA・#TT）。
                resources[system] = parse_resource_csv(
                    csv_path, container_prefix=f"{system}-",
                )

        condition["loss"] = loss_data
        condition["resources"] = resources
        runs.append(condition)

    return runs


def compute_stats(values: list[float]) -> dict[str, Any]:
    """平均・標準偏差・最小・最大を算出する。"""
    if not values:
        return {"mean": None, "std": None, "min": None, "max": None, "n": 0}
    arr = np.array(values)
    n = len(arr)
    std = float(arr.std(ddof=1)) if n > 1 else 0.0
    return {
        "mean": round(float(arr.mean()), 4),
        "std": round(std, 4),
        "min": round(float(arr.min()), 4),
        "max": round(float(arr.max()), 4),
        "n": n,
    }


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """条件ごとに統計集約する。"""
    groups = group_runs(runs)

    summary = []
    for (pattern, rps), trials in sorted(groups.items()):
        entry = {
            "pattern": pattern,
            "rps": rps,
            "n_trials": len(trials),
        }

        for system in ["baseline", "proposed"]:
            loss_values = []
            cpu_peaks = []
            mem_peaks = []
            n_verification_failed = 0

            for t in trials:
                loss_data = t["loss"].get(system, {})
                if loss_data.get("verification_failed"):
                    # 検証クエリ自体の失敗（例: ES 429）による0件は真の欠損と区別し、
                    # 統計集計からは除外する（欠損の隠蔽ではなく、別カウントで開示する）。
                    n_verification_failed += 1
                else:
                    loss = loss_data.get("loss_rate_percent")
                    if loss is not None:
                        loss_values.append(float(loss))

                res = t["resources"].get(system, {})
                if res.get("cpu_peak") is not None:
                    cpu_peaks.append(res["cpu_peak"])
                if res.get("mem_peak") is not None:
                    mem_peaks.append(res["mem_peak"])

            entry[system] = {
                "loss_rate": compute_stats(loss_values),
                "cpu_peak": compute_stats(cpu_peaks),
                "mem_peak": compute_stats(mem_peaks),
                "n_verification_failed": n_verification_failed,
            }

        summary.append(entry)

    return {"conditions": summary, "total_runs": len(runs)}


def print_summary_table(summary: dict[str, Any]) -> None:
    """集約結果をテーブル形式で出力する。"""
    print("\n=== 欠損率サマリー ===")
    print(
        f"{'pattern':<8} {'RPS':>6} {'n':>3}  "
        f"{'Baseline Loss(%)':>18}  "
        f"{'Proposed Loss(%)':>18}"
    )
    print("-" * 70)

    for c in summary["conditions"]:
        bl = c["baseline"]["loss_rate"]
        pl = c["proposed"]["loss_rate"]
        bl_str = f"{bl['mean']:.2f} ± {bl['std']:.2f}" if bl["mean"] is not None else "N/A"
        pl_str = f"{pl['mean']:.2f} ± {pl['std']:.2f}" if pl["mean"] is not None else "N/A"
        print(
            f"{c['pattern']:<8} {c['rps']:>6} {c['n_trials']:>3}  "
            f"{bl_str:>18}  "
            f"{pl_str:>18}"
        )

    print("\n=== リソース消費サマリー (Peak CPU %) ===")
    print(
        f"{'pattern':<8} {'RPS':>6}  "
        f"{'Baseline CPU(%)':>18}  "
        f"{'Proposed CPU(%)':>18}"
    )
    print("-" * 60)

    for c in summary["conditions"]:
        bc = c["baseline"]["cpu_peak"]
        pc = c["proposed"]["cpu_peak"]
        bc_str = f"{bc['mean']:.1f} ± {bc['std']:.1f}" if bc["mean"] is not None else "N/A"
        pc_str = f"{pc['mean']:.1f} ± {pc['std']:.1f}" if pc["mean"] is not None else "N/A"
        print(
            f"{c['pattern']:<8} {c['rps']:>6}  "
            f"{bc_str:>18}  "
            f"{pc_str:>18}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="バッチ実験結果の統計集約",
    )
    parser.add_argument("--batch-dir", required=True, help="バッチ結果ディレクトリ")
    parser.add_argument("--output", default=None, help="集約結果 JSON 出力先")

    args = parser.parse_args()

    if not os.path.isdir(args.batch_dir):
        print(f"[ERROR] ディレクトリが見つかりません: {args.batch_dir}", file=sys.stderr)
        sys.exit(1)

    runs = scan_batch_dir(args.batch_dir)
    if not runs:
        print("[ERROR] 結果データが見つかりません", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] {len(runs)} 件の試行結果を検出")
    summary = aggregate(runs)
    print_summary_table(summary)

    output_path = args.output or os.path.join(args.batch_dir, "summary.json")
    ensure_parent_dir(output_path)
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n[INFO] 集約結果を保存: {output_path}")


if __name__ == "__main__":
    main()

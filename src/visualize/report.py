# self-proving-observation/
# └── src/
#     └── visualize/
#         └── report.py  — 実験結果の可視化・レポート生成

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

# `python3 src/visualize/report.py` のように直接パス実行された場合、
# プロジェクトルートが sys.path に乗らず `from src...` の import が失敗する。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # noqa: E402

from src.logging_config import ensure_utf8_stdio  # noqa: E402
from src.measure._common import ensure_parent_dir  # noqa: E402

ensure_utf8_stdio()

RESULTS_DIR = "results"


def plot_resource_comparison(
    baseline_csv: str,
    proposed_csv: str,
    output_path: str | None = None,
) -> None:
    """第1号機 vs 第2号機のリソース消費時系列を比較プロットする。"""
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

    any_data = False
    for csv_path, label, color in [
        (baseline_csv, "Baseline (ELK)", "#e74c3c"),
        (proposed_csv, "Proposed (Vector/FastAPI)", "#2ecc71"),
    ]:
        timestamps, cpu_values, mem_values = [], [], []
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    cpu = float(row["cpu_percent"])
                    mem = float(row["mem_percent"])
                except (ValueError, KeyError):
                    continue
                timestamps.append(row.get("timestamp", ""))
                cpu_values.append(cpu)
                mem_values.append(mem)

        if not cpu_values:
            print(f"[WARN] {csv_path}: データなし（Dockerコンテナ未起動または psutil 未インストール）")  # noqa: E501
            continue

        any_data = True
        x = range(len(timestamps))
        axes[0].plot(x, cpu_values, label=label, color=color, alpha=0.8)
        axes[1].plot(x, mem_values, label=label, color=color, alpha=0.8)

    if not any_data:
        plt.close()
        print("[WARN] リソース CSV にデータがありません。グラフ生成をスキップします。")
        return

    axes[0].set_ylabel("CPU Usage (%)")
    axes[0].set_title("Resource Consumption: Baseline vs Proposed")
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)

    axes[1].set_ylabel("Memory Usage (%)")
    axes[1].set_xlabel("Time (seconds)")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    out = output_path or os.path.join(RESULTS_DIR, "resource_comparison.png")
    ensure_parent_dir(out)
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"[VIZ] Resource comparison saved to {out}")


def plot_loss_rate(
    loss_rate_json: str,
    output_path: str | None = None,
) -> None:
    """欠損率の比較棒グラフを生成する。"""
    with open(loss_rate_json) as f:
        data = json.load(f)

    nodes = list(data.keys())
    loss_rates = [data[n]["loss_rate_percent"] for n in nodes]
    injected = [data[n]["total_injected"] for n in nodes]
    recorded = [data[n]["total_recorded"] for n in nodes]
    # verification_failed（検証クエリ自体の失敗）が立っている値は、真の欠損率
    # ではなく「検証できなかった」ことを意味する（src/measure/loss_rate.py）。
    # batch_report.pyの集計は既にこのフラグの立った試行を除外しているが、
    # 単発実験用の本グラフはこれまで値をそのまま描画しており、検証失敗を
    # 確認済みの欠損率であるかのように誤読させうる
    # （「欠損の隠蔽をしない」原則に反する）。値は隠さず表示しつつ、
    # ハッチングとラベルで「未検証」であることを明示する。
    unverified = [bool(data[n].get("verification_failed")) for n in nodes]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    colors = ["#e74c3c" if "baseline" in n else "#2ecc71" for n in nodes]
    bars = ax1.bar(nodes, loss_rates, color=colors)
    for bar, uv in zip(bars, unverified, strict=True):
        if uv:
            bar.set_hatch("//")
            bar.set_edgecolor("black")
    ax1.set_ylabel("Loss Rate (%)")
    ax1.set_title("Data Loss Rate Comparison")
    for i, (v, uv) in enumerate(zip(loss_rates, unverified, strict=True)):
        label = f"{v}% (unverified)" if uv else f"{v}%"
        ax1.text(i, v + 0.1, label, ha="center", fontweight="bold")
    ax1.grid(True, alpha=0.3, axis="y")

    x = np.arange(len(nodes))
    width = 0.35
    ax2.bar(x - width / 2, injected, width, label="Injected", color="#3498db")
    ax2.bar(x + width / 2, recorded, width, label="Recorded", color="#2ecc71")
    ax2.set_ylabel("Event Count")
    ax2.set_title("Injected vs Recorded Events")
    ax2.set_xticks(x)
    ax2.set_xticklabels(nodes)
    ax2.legend()
    ax2.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    out = output_path or os.path.join(RESULTS_DIR, "loss_rate_comparison.png")
    ensure_parent_dir(out)
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"[VIZ] Loss rate comparison saved to {out}")


def plot_fidelity_timeline(
    fidelity_results: list[dict[str, Any]],
    output_path: str | None = None,
) -> None:
    """Fidelity スコアの時系列プロットを生成する。"""
    windows = [r["window_index"] for r in fidelity_results]
    scores = [r["fidelity_score"] for r in fidelity_results]
    moving_avgs = [r["moving_avg"] for r in fidelity_results]
    drift_points = [r["window_index"] for r in fidelity_results if r["drift_detected"]]

    fig, ax = plt.subplots(figsize=(14, 5))

    ax.plot(windows, scores, "o-", markersize=3, label="Fidelity Score", color="#3498db", alpha=0.6)
    ax.plot(windows, moving_avgs, "-", linewidth=2, label="Moving Average (5)", color="#e67e22")

    for i, dp in enumerate(drift_points):
        ax.axvline(
            x=dp, color="#e74c3c", linestyle="--", alpha=0.5,
            label="Drift Detected" if i == 0 else None,
        )

    ax.axhline(y=0.5, color="#95a5a6", linestyle=":", alpha=0.5, label="Threshold")
    ax.set_xlabel("Time Window Index")
    ax.set_ylabel("Fidelity Score")
    ax.set_title("Fidelity Guard — Concept Drift Detection")
    ax.set_ylim(-0.05, 1.05)
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = output_path or os.path.join(RESULTS_DIR, "fidelity_timeline.png")
    ensure_parent_dir(out)
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"[VIZ] Fidelity timeline saved to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="実験結果レポート生成",
    )
    parser.add_argument("--results-dir", required=True, help="実験結果ディレクトリ")
    args = parser.parse_args()

    results_dir = args.results_dir
    baseline_csv = os.path.join(results_dir, "resource_baseline.csv")
    proposed_csv = os.path.join(results_dir, "resource_proposed.csv")
    loss_json = os.path.join(results_dir, "loss_rate.json")

    if os.path.exists(baseline_csv) and os.path.exists(proposed_csv):
        plot_resource_comparison(
            baseline_csv, proposed_csv,
            os.path.join(results_dir, "resource_comparison.png"),
        )
    else:
        print(f"[WARN] リソース CSV が見つかりません: {baseline_csv}, {proposed_csv}")

    if os.path.exists(loss_json):
        plot_loss_rate(loss_json, os.path.join(results_dir, "loss_rate_comparison.png"))
    else:
        print(f"[WARN] 欠損率 JSON が見つかりません: {loss_json}")


if __name__ == "__main__":
    main()

# self-proving-observation/
# └── src/
#     └── visualize/
#         └── batch_report.py  — 論文用バッチ実験可視化（箱ひげ図・ヒートマップ）

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

# `python3 src/visualize/batch_report.py` のように直接パス実行された場合、
# プロジェクトルートが sys.path に乗らず `from src...` の import が失敗する。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # noqa: E402

from src.logging_config import ensure_utf8_stdio  # noqa: E402
from src.measure._common import group_runs as _group_runs  # noqa: E402
from src.measure.aggregate import scan_batch_dir  # noqa: E402

ensure_utf8_stdio()


def _extract_values(trials: list[dict[str, Any]], path: list[str]) -> list[float]:
    """path で指定した値を各trialから抽出する。

    末端値の親辞書に verification_failed が立っている場合はスキップする
    （検証クエリ自体の失敗による0件を、真の欠損率と混同して集計しないため）。
    """
    values = []
    for t in trials:
        obj = t
        for key in path[:-1]:
            obj = obj.get(key, {}) if isinstance(obj, dict) else {}
        if not isinstance(obj, dict) or obj.get("verification_failed"):
            continue
        val = obj.get(path[-1])
        if isinstance(val, (int, float)):
            values.append(float(val))
    return values


def plot_loss_boxplots(runs: list[dict[str, Any]], output_dir: str) -> None:
    """RPS レベル別の欠損率箱ひげ図を生成する。"""
    groups = _group_runs(runs)
    rps_levels = sorted(set(rps for _, rps in groups.keys()))

    fig, axes = plt.subplots(1, len(rps_levels), figsize=(4 * len(rps_levels), 6), sharey=True)
    if len(rps_levels) == 1:
        axes = [axes]

    bp1, bp2 = None, None
    for ax, rps in zip(axes, rps_levels, strict=False):
        baseline_data = []
        proposed_data = []
        patterns = []

        for pattern in sorted(set(p for p, _ in groups.keys())):
            trials = groups.get((pattern, rps), [])
            if not trials:
                continue
            patterns.append(pattern)
            baseline_data.append(
                _extract_values(trials, ["loss", "baseline", "loss_rate_percent"])
            )
            proposed_data.append(
                _extract_values(trials, ["loss", "proposed", "loss_rate_percent"])
            )

        if not patterns:
            continue

        x = np.arange(len(patterns))
        width = 0.35

        bp1 = ax.boxplot(
            baseline_data, positions=x - width / 2, widths=width * 0.8,
            patch_artist=True, boxprops=dict(facecolor="#e74c3c", alpha=0.6),
            medianprops=dict(color="black"),
        )
        bp2 = ax.boxplot(
            proposed_data, positions=x + width / 2, widths=width * 0.8,
            patch_artist=True, boxprops=dict(facecolor="#2ecc71", alpha=0.6),
            medianprops=dict(color="black"),
        )

        ax.set_title(f"RPS = {rps}")
        ax.set_xticks(x)
        ax.set_xticklabels(patterns, rotation=45, ha="right")
        ax.grid(True, alpha=0.3, axis="y")

    axes[0].set_ylabel("Loss Rate (%)")
    if bp1 is not None and bp2 is not None:
        axes[0].legend(
            [bp1["boxes"][0], bp2["boxes"][0]],
            ["Baseline (ELK)", "Proposed"],
            loc="upper left",
        )

    fig.suptitle("Data Loss Rate by RPS Level and Pattern", fontsize=14)
    plt.tight_layout()

    out = os.path.join(output_dir, "boxplot_loss_rate.png")
    plt.savefig(out, dpi=200, bbox_inches="tight")
    plt.close()
    print(f"[VIZ] Loss rate boxplot saved to {out}")


def plot_resource_boxplots(runs: list[dict[str, Any]], output_dir: str) -> None:
    """RPS レベル別のリソース消費箱ひげ図を生成する。"""
    groups = _group_runs(runs)
    rps_levels = sorted(set(rps for _, rps in groups.keys()))

    fig, axes = plt.subplots(2, len(rps_levels), figsize=(4 * len(rps_levels), 10), sharey="row")
    if len(rps_levels) == 1:
        axes = axes.reshape(-1, 1)

    bp1, bp2 = None, None
    for col, rps in enumerate(rps_levels):
        metrics = [("cpu_peak", "Peak CPU (%)"), ("mem_peak", "Peak Memory (%)")]
        for row, (metric, label) in enumerate(metrics):
            ax = axes[row][col]
            baseline_data = []
            proposed_data = []
            patterns = []

            for pattern in sorted(set(p for p, _ in groups.keys())):
                trials = groups.get((pattern, rps), [])
                if not trials:
                    continue
                patterns.append(pattern)
                baseline_data.append(
                    _extract_values(trials, ["resources", "baseline", metric])
                )
                proposed_data.append(
                    _extract_values(trials, ["resources", "proposed", metric])
                )

            if not patterns:
                continue

            x = np.arange(len(patterns))
            width = 0.35

            bp1 = ax.boxplot(
                baseline_data, positions=x - width / 2, widths=width * 0.8,
                patch_artist=True, boxprops=dict(facecolor="#e74c3c", alpha=0.6),
                medianprops=dict(color="black"),
            )
            bp2 = ax.boxplot(
                proposed_data, positions=x + width / 2, widths=width * 0.8,
                patch_artist=True, boxprops=dict(facecolor="#2ecc71", alpha=0.6),
                medianprops=dict(color="black"),
            )

            if col == 0:
                ax.set_ylabel(label)
            if row == 0:
                ax.set_title(f"RPS = {rps}")
            ax.set_xticks(x)
            ax.set_xticklabels(patterns, rotation=45, ha="right")
            ax.grid(True, alpha=0.3, axis="y")

    if bp1 is not None and bp2 is not None:
        axes[0][0].legend(
            [bp1["boxes"][0], bp2["boxes"][0]],
            ["Baseline (ELK)", "Proposed"],
            loc="upper left",
        )

    fig.suptitle("Resource Consumption by RPS Level and Pattern", fontsize=14)
    plt.tight_layout()

    out = os.path.join(output_dir, "boxplot_resources.png")
    plt.savefig(out, dpi=200, bbox_inches="tight")
    plt.close()
    print(f"[VIZ] Resource boxplot saved to {out}")


def plot_loss_heatmap(runs: list[dict[str, Any]], output_dir: str) -> None:
    """欠損率のヒートマップ（Pattern × RPS）を生成する。"""
    groups = _group_runs(runs)
    patterns = sorted(set(p for p, _ in groups.keys()))
    rps_levels = sorted(set(r for _, r in groups.keys()))

    for system, title, cmap in [
        ("baseline", "Baseline (ELK) — Mean Loss Rate (%)", "Reds"),
        ("proposed", "Proposed — Mean Loss Rate (%)", "Greens"),
    ]:
        matrix = np.full((len(patterns), len(rps_levels)), np.nan)

        for i, pattern in enumerate(patterns):
            for j, rps in enumerate(rps_levels):
                trials = groups.get((pattern, rps), [])
                vals = _extract_values(trials, ["loss", system, "loss_rate_percent"])
                if vals:
                    matrix[i, j] = np.mean(vals)

        fig, ax = plt.subplots(figsize=(max(8, len(rps_levels) * 1.5), max(4, len(patterns) * 1.2)))
        im = ax.imshow(matrix, cmap=cmap, aspect="auto")

        ax.set_xticks(range(len(rps_levels)))
        ax.set_xticklabels([str(r) for r in rps_levels])
        ax.set_yticks(range(len(patterns)))
        ax.set_yticklabels(patterns)
        ax.set_xlabel("RPS")
        ax.set_ylabel("Pattern")
        ax.set_title(title)

        for i in range(len(patterns)):
            for j in range(len(rps_levels)):
                val = matrix[i, j]
                if not np.isnan(val):
                    thresh = matrix[~np.isnan(matrix)].max() * 0.6
                    color = "white" if val > thresh else "black"
                    ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                            color=color, fontsize=10, fontweight="bold")

        plt.colorbar(im, ax=ax, label="Loss Rate (%)")
        plt.tight_layout()

        out = os.path.join(output_dir, f"heatmap_loss_{system}.png")
        plt.savefig(out, dpi=200, bbox_inches="tight")
        plt.close()
        print(f"[VIZ] Heatmap saved to {out}")


def plot_throughput_curve(runs: list[dict[str, Any]], output_dir: str) -> None:
    """スループット曲線（RPS vs 欠損率）を生成する。"""
    groups = _group_runs(runs)
    patterns = sorted(set(p for p, _ in groups.keys()))
    rps_levels = sorted(set(r for _, r in groups.keys()))

    fig, ax = plt.subplots(figsize=(10, 6))

    for system, color, marker in [("baseline", "#e74c3c", "o"), ("proposed", "#2ecc71", "s")]:
        for pattern in patterns:
            rps_list = []
            means = []
            stds = []
            for rps in rps_levels:
                trials = groups.get((pattern, rps), [])
                vals = _extract_values(trials, ["loss", system, "loss_rate_percent"])
                if vals:
                    rps_list.append(rps)
                    means.append(np.mean(vals))
                    stds.append(np.std(vals, ddof=1) if len(vals) > 1 else 0.0)

            if rps_list:
                label = f"{system.capitalize()} ({pattern})"
                ax.errorbar(rps_list, means, yerr=stds, label=label,
                            color=color, marker=marker, alpha=0.7, capsize=3,
                            linestyle="--" if pattern != "spike" else "-")

    ax.set_xlabel("Requests per Second (RPS)")
    ax.set_ylabel("Loss Rate (%)")
    ax.set_title("Throughput Curve: Loss Rate vs RPS")
    ax.set_xscale("log")
    ax.legend(fontsize=8, ncol=2)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = os.path.join(output_dir, "throughput_curve.png")
    plt.savefig(out, dpi=200, bbox_inches="tight")
    plt.close()
    print(f"[VIZ] Throughput curve saved to {out}")


def generate_batch_report(batch_dir: str, output_dir: str | None = None) -> None:
    """バッチ実験結果から全可視化を生成する。"""
    runs = scan_batch_dir(batch_dir)
    if not runs:
        print("[ERROR] 結果データが見つかりません", file=sys.stderr)
        return

    out_dir = output_dir or os.path.join(batch_dir, "figures")
    os.makedirs(out_dir, exist_ok=True)

    print(f"[VIZ] {len(runs)} 件の試行結果から可視化を生成...")
    plot_loss_boxplots(runs, out_dir)
    plot_resource_boxplots(runs, out_dir)
    plot_loss_heatmap(runs, out_dir)
    plot_throughput_curve(runs, out_dir)
    print(f"[VIZ] 全図表を {out_dir} に保存")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="論文用バッチ可視化",
    )
    parser.add_argument("--batch-dir", required=True, help="バッチ結果ディレクトリ")
    parser.add_argument("--output-dir", default=None, help="図表出力ディレクトリ")

    args = parser.parse_args()

    if not os.path.isdir(args.batch_dir):
        print(f"[ERROR] ディレクトリが見つかりません: {args.batch_dir}", file=sys.stderr)
        sys.exit(1)

    generate_batch_report(args.batch_dir, args.output_dir)


if __name__ == "__main__":
    main()

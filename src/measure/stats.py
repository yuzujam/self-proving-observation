# self-proving-observation/
# └── src/
#     └── measure/
#         └── stats.py  — 論文用統計検定（Mann-Whitney U, 効果量, 信頼区間）

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats as sp_stats

# `python3 src/measure/stats.py` のように直接パス実行された場合、
# プロジェクトルートが sys.path に乗らず `from src...` の import が失敗する。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.logging_config import ensure_utf8_stdio
from src.measure._common import (
    ensure_parent_dir,
    group_runs,
    latex_table_footer,
    latex_table_header,
)
from src.measure.aggregate import scan_batch_dir

ensure_utf8_stdio()


def mann_whitney_test(
    baseline: list[float],
    proposed: list[float],
) -> dict[str, Any]:
    """Mann-Whitney U 検定（Wilcoxon rank-sum）を実行する。"""
    if len(baseline) < 2 or len(proposed) < 2:
        return {
            "U": None, "p_value": None, "effect_size_r": None,
            "n_baseline": len(baseline), "n_proposed": len(proposed),
        }

    if (
        all(b == baseline[0] for b in baseline)
        and all(p == proposed[0] for p in proposed)
        and baseline[0] == proposed[0]
    ):
        return {
            "U": None, "p_value": 1.0, "effect_size_r": 0.0,
            "n_baseline": len(baseline), "n_proposed": len(proposed),
        }

    U, p = sp_stats.mannwhitneyu(baseline, proposed, alternative="two-sided")
    n1, n2 = len(baseline), len(proposed)
    r = 1.0 - (2.0 * U) / (n1 * n2)

    return {
        "U": float(U),
        "p_value": round(float(p), 6),
        "effect_size_r": round(float(r), 4),
        "n_baseline": n1,
        "n_proposed": n2,
    }


def fisher_exact_loss_occurrence(
    baseline: list[float],
    proposed: list[float],
) -> dict[str, Any]:
    """欠損が1件でも発生した試行の割合をFisherの正確検定で比較する。

    mann_whitney_test（連続値としての欠損率比較）を置き換えるものではなく、
    仮説の実体（proposedは構造的に欠損しない）に対応する二項検定として追加する。
    proposedのloss_rateが多くの試行で0.00%に張り付き分散がほぼゼロになる場合、
    連続値の順位検定では効果量が薄まりやすいため、「欠損が起きたか否か」という
    二値の発生率に着目することで同じデータからより高い検出力を得られる場合がある。
    """
    n_baseline, n_proposed = len(baseline), len(proposed)
    if n_baseline == 0 or n_proposed == 0:
        return {
            "odds_ratio": None, "p_value": None,
            "n_baseline": n_baseline, "n_proposed": n_proposed,
            "baseline_loss_count": 0, "proposed_loss_count": 0,
        }

    baseline_loss = sum(1 for v in baseline if v > 0)
    proposed_loss = sum(1 for v in proposed if v > 0)
    table = [
        [baseline_loss, n_baseline - baseline_loss],
        [proposed_loss, n_proposed - proposed_loss],
    ]

    odds_ratio, p = sp_stats.fisher_exact(table, alternative="two-sided")

    return {
        "odds_ratio": round(float(odds_ratio), 4) if np.isfinite(odds_ratio) else None,
        "p_value": round(float(p), 6),
        "baseline_loss_count": baseline_loss,
        "baseline_no_loss_count": n_baseline - baseline_loss,
        "proposed_loss_count": proposed_loss,
        "proposed_no_loss_count": n_proposed - proposed_loss,
        "n_baseline": n_baseline,
        "n_proposed": n_proposed,
    }


def cochran_armitage_trend_test(
    rps_levels: list[float],
    ns: list[int],
    xs: list[int],
) -> dict[str, Any]:
    """欠損発生率のRPSに対する用量反応傾向をCochran-Armitage検定で評価する。

    mann_whitney_test・fisher_exact_loss_occurrenceはRPS条件ごとの独立した
    2群比較であり、20条件への多重比較補正後は`ramp/wave×rps=5000`の2条件
    のみ有意という局所的な結果になる。本関数は
    それらを置き換えるものではなく、「RPSが上がるほど欠損発生率が単調に
    増加するか」という用量反応関係をパターン単位（RPS軸横断）で追加検証する
    ために新設する。

    スコアは実測RPS値ではなく等間隔ランク（0,1,2,...）を用いる。実験のRPS水準
    （100/500/1000/2000/5000）は等間隔でないため、生のRPS値をスコアに使うと
    最大水準（5000）の間隔が過大に効いてしまうため。
    """
    if len(rps_levels) < 3:
        return {
            "z_statistic": None,
            "p_value_two_sided": None,
            "p_value_one_sided_increasing": None,
            "n_levels": len(rps_levels),
            "rps_levels": list(rps_levels),
        }

    order = np.argsort(rps_levels)
    rps_sorted = np.array(rps_levels)[order]
    ns_arr = np.array(ns, dtype=float)[order]
    xs_arr = np.array(xs, dtype=float)[order]
    scores = np.arange(len(rps_sorted), dtype=float)

    n_total = ns_arr.sum()
    pbar = xs_arr.sum() / n_total
    tbar = (ns_arr * scores).sum() / n_total
    p_i = xs_arr / ns_arr

    numerator = (ns_arr * (p_i - pbar) * (scores - tbar)).sum()
    variance = pbar * (1.0 - pbar) * (ns_arr * (scores - tbar) ** 2).sum()

    if variance <= 0:
        z = 0.0
        p_two_sided = 1.0
        p_one_sided = 1.0
    else:
        z = numerator / np.sqrt(variance)
        p_two_sided = 2.0 * (1.0 - sp_stats.norm.cdf(abs(z)))
        p_one_sided = 1.0 - sp_stats.norm.cdf(z)

    return {
        "z_statistic": round(float(z), 4),
        "p_value_two_sided": round(float(p_two_sided), 6),
        "p_value_one_sided_increasing": round(float(p_one_sided), 6),
        "n_levels": len(rps_sorted),
        "rps_levels": [float(v) for v in rps_sorted],
        "occurrence_rate_by_rps": {
            float(r): round(float(x / n), 4)
            for r, n, x in zip(rps_sorted, ns_arr, xs_arr, strict=True)
        },
    }


def trend_test_by_pattern(conditions: list[dict[str, Any]]) -> dict[str, Any]:
    """`analyze_batch_results`/`pool_control_experiment_stats.py`が生成する
    conditionsリスト（pattern×rps条件ごとのエントリ）から、パターン単位で
    RPS軸を横断したCochran-Armitage傾向検定を行う。baseline側の欠損発生率
    （`occurrence_test`）を用いる。
    """
    by_pattern: dict[str, list[tuple[float, int, int]]] = {}
    for c in conditions:
        occ = c.get("loss_rate", {}).get("occurrence_test", {})
        n_b = occ.get("n_baseline")
        x_b = occ.get("baseline_loss_count")
        if not n_b:
            continue
        by_pattern.setdefault(c["pattern"], []).append((c["rps"], n_b, x_b))

    result = {}
    for pattern, rows in by_pattern.items():
        rps_levels: list[float] = [r[0] for r in rows]
        ns: list[int] = [r[1] for r in rows]
        xs: list[int] = [r[2] for r in rows]
        result[pattern] = cochran_armitage_trend_test(rps_levels, ns, xs)
    return result


def kruskal_wallis_test(groups: dict[str, list[float]]) -> dict[str, Any]:
    """独立した3群以上の分布差をKruskal-Wallis検定（順位に基づく分散分析）で評価する。

    攻撃周期パターン（5.3節・6.3章）の曜日別・任意周期ビン別比較のために新設。
    mann_whitney_test（2群比較）を置き換えるものではなく、3群以上（曜日=7群、
    24日周期ビン=24群等）を一度に比較する場合に用いる。空でない群が2未満の場合は
    検定不能としてNoneを返す（`src.measure.periodicity`のgroup_by_weekday /
    group_by_cycle_phaseが返す辞書をそのまま渡せる）。
    """
    non_empty = {k: v for k, v in groups.items() if len(v) >= 1}
    if len(non_empty) < 2:
        return {
            "H_statistic": None,
            "p_value": None,
            "n_groups": len(non_empty),
            "group_n": {k: len(v) for k, v in groups.items()},
            "group_mean": {k: round(float(np.mean(v)), 2) for k, v in non_empty.items()},
        }

    all_values = [v for values in non_empty.values() for v in values]
    if all(v == all_values[0] for v in all_values):
        # 全群・全値が同一（完全な同値）だと順位に基づく分散がゼロになり、
        # scipy.stats.kruskalのタイ補正が0除算でNaNを返す（mann_whitney_testの
        # 同種ガードと同じ理由）。この場合は分布差なしとしてp=1.0を明示する。
        return {
            "H_statistic": 0.0,
            "p_value": 1.0,
            "n_groups": len(non_empty),
            "group_n": {k: len(v) for k, v in groups.items()},
            "group_mean": {k: round(float(np.mean(v)), 2) for k, v in non_empty.items()},
        }

    H, p = sp_stats.kruskal(*non_empty.values())

    return {
        "H_statistic": round(float(H), 4),
        "p_value": round(float(p), 6),
        "n_groups": len(non_empty),
        "group_n": {k: len(v) for k, v in groups.items()},
        "group_mean": {k: round(float(np.mean(v)), 2) for k, v in non_empty.items()},
    }


def bootstrap_ci(
    values: list[float],
    confidence: float = 0.95,
    n_bootstrap: int = 10000,
) -> dict[str, Any]:
    """ブートストラップ法で信頼区間を算出する。"""
    if len(values) < 2:
        return {"lower": None, "upper": None, "mean": None, "confidence": confidence}

    arr = np.array(values)
    rng = np.random.default_rng(42)
    boot_means = np.array([
        rng.choice(arr, size=len(arr), replace=True).mean()
        for _ in range(n_bootstrap)
    ])

    alpha = 1.0 - confidence
    lower = float(np.percentile(boot_means, 100 * alpha / 2))
    upper = float(np.percentile(boot_means, 100 * (1 - alpha / 2)))

    return {
        "lower": round(lower, 4),
        "upper": round(upper, 4),
        "mean": round(float(arr.mean()), 4),
        "confidence": confidence,
    }


def wilson_score_interval(successes: int, n: int, confidence: float = 0.95) -> dict[str, Any]:
    """Wilson score interval で二項比率の信頼区間を算出する。"""
    if n == 0:
        return {
            "lower": None, "upper": None, "rate": None,
            "n": 0, "successes": 0, "confidence": confidence,
        }

    z = float(sp_stats.norm.ppf(1.0 - (1.0 - confidence) / 2.0))
    phat = successes / n
    denom = 1.0 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * ((phat * (1.0 - phat) / n + z * z / (4 * n * n)) ** 0.5)

    return {
        "lower": round((center - margin) / denom * 100, 1),
        "upper": round((center + margin) / denom * 100, 1),
        "rate": round(phat * 100, 1),
        "n": n,
        "successes": successes,
        "confidence": confidence,
    }


def analyze_condition(
    trials: list[dict[str, Any]], system_key: str, metric_key: str,
) -> list[float]:
    """1条件の全試行から指定メトリクスの値リストを抽出する。

    loss_rate_percentは、検証クエリ自体の失敗（例: ES 429、`verification_failed`）
    による値を真の欠損と区別し統計検定から除外する（`src/measure/aggregate.py`の
    集計と同じ扱い。欠損の記録を最優先する）。除外件数は
    `analyze_batch_results`側で`n_verification_failed`として別途開示する。
    """
    values = []
    for t in trials:
        if metric_key == "loss_rate_percent":
            loss_data = t["loss"].get(system_key, {})
            if loss_data.get("verification_failed"):
                continue
            val = loss_data.get(metric_key)
        else:
            val = t["resources"].get(system_key, {}).get(metric_key)
        if val is not None:
            values.append(float(val))
    return values


def _count_verification_failed(trials: list[dict[str, Any]], system_key: str) -> int:
    return sum(1 for t in trials if t["loss"].get(system_key, {}).get("verification_failed"))


def analyze_batch_results(batch_dir: str) -> dict[str, Any]:
    """バッチ実験結果に統計検定を実行する。"""
    runs = scan_batch_dir(batch_dir)
    if not runs:
        print("[ERROR] 結果データが見つかりません", file=sys.stderr)
        return {}

    groups = group_runs(runs)

    conditions = []
    for (pattern, rps), trials in sorted(groups.items()):
        entry = {
            "pattern": pattern,
            "rps": rps,
            "n_trials": len(trials),
        }

        for metric_key, metric_label in [
            ("loss_rate_percent", "loss_rate"),
            ("cpu_peak", "cpu_peak"),
            ("mem_peak", "mem_peak"),
        ]:
            baseline_vals = analyze_condition(trials, "baseline", metric_key)
            proposed_vals = analyze_condition(trials, "proposed", metric_key)

            test_result = mann_whitney_test(baseline_vals, proposed_vals)
            baseline_ci = bootstrap_ci(baseline_vals)
            proposed_ci = bootstrap_ci(proposed_vals)

            entry[metric_label] = {
                "test": test_result,
                "baseline_ci": baseline_ci,
                "proposed_ci": proposed_ci,
                "baseline_raw": baseline_vals,
                "proposed_raw": proposed_vals,
            }
            if metric_key == "loss_rate_percent":
                entry[metric_label]["n_verification_failed"] = {
                    "baseline": _count_verification_failed(trials, "baseline"),
                    "proposed": _count_verification_failed(trials, "proposed"),
                }
                entry[metric_label]["occurrence_test"] = fisher_exact_loss_occurrence(
                    baseline_vals, proposed_vals
                )

        conditions.append(entry)

    return {
        "conditions": conditions,
        "total_runs": len(runs),
        "trend_by_pattern": trend_test_by_pattern(conditions),
    }


def export_latex_loss_table(stats_results: dict[str, Any], output_path: str) -> None:
    """欠損率の比較を LaTeX 表として出力する。"""
    ensure_parent_dir(output_path)

    lines = latex_table_header(
        caption=r"欠損率の比較 (\%)",
        label="tab:loss-rate",
        col_spec="llrrrrrr",
        header_rows=[
            r"Pattern & RPS & \multicolumn{2}{c}{Baseline}"
            r" & \multicolumn{2}{c}{Proposed} & $p$ & $r$ \\",
            r" & & Mean & SD & Mean & SD & & \\",
        ],
    )

    for c in stats_results["conditions"]:
        lr = c["loss_rate"]
        bl_ci = lr["baseline_ci"]
        pr_ci = lr["proposed_ci"]
        test = lr["test"]

        bl_mean = f"{bl_ci['mean']:.2f}" if bl_ci["mean"] is not None else "---"
        bl_std = f"{_compute_std(lr['baseline_raw']):.2f}" if lr["baseline_raw"] else "---"
        pr_mean = f"{pr_ci['mean']:.2f}" if pr_ci["mean"] is not None else "---"
        pr_std = f"{_compute_std(lr['proposed_raw']):.2f}" if lr["proposed_raw"] else "---"
        p_val = f"{test['p_value']:.3f}" if test["p_value"] is not None else "---"
        r_val = f"{test['effect_size_r']:.2f}" if test["effect_size_r"] is not None else "---"

        p_marker = ""
        if test["p_value"] is not None:
            if test["p_value"] < 0.001:
                p_marker = "***"
            elif test["p_value"] < 0.01:
                p_marker = "**"
            elif test["p_value"] < 0.05:
                p_marker = "*"

        lines.append(
            f"{c['pattern']} & {c['rps']} & {bl_mean} & {bl_std} "
            f"& {pr_mean} & {pr_std} & {p_val}{p_marker} & {r_val} \\\\"
        )

    lines.extend(latex_table_footer(notes=[
        r"$p$: Mann-Whitney U 検定, $r$: rank-biserial 効果量",
        r"*$p<.05$, **$p<.01$, ***$p<.001$",
    ]))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[STATS] LaTeX table saved to {output_path}")


def export_latex_resource_table(stats_results: dict[str, Any], output_path: str) -> None:
    """リソース消費の比較を LaTeX 表として出力する。"""
    ensure_parent_dir(output_path)

    lines = latex_table_header(
        caption=r"ピークCPU使用率の比較 (\%)",
        label="tab:cpu-peak",
        col_spec="llrrrrrr",
        header_rows=[
            r"Pattern & RPS & \multicolumn{2}{c}{Baseline}"
            r" & \multicolumn{2}{c}{Proposed} & $p$ & $r$ \\",
            r" & & Mean & SD & Mean & SD & & \\",
        ],
    )

    for c in stats_results["conditions"]:
        cp = c["cpu_peak"]
        bl_ci = cp["baseline_ci"]
        pr_ci = cp["proposed_ci"]
        test = cp["test"]

        bl_mean = f"{bl_ci['mean']:.1f}" if bl_ci["mean"] is not None else "---"
        bl_std = f"{_compute_std(cp['baseline_raw']):.1f}" if cp["baseline_raw"] else "---"
        pr_mean = f"{pr_ci['mean']:.1f}" if pr_ci["mean"] is not None else "---"
        pr_std = f"{_compute_std(cp['proposed_raw']):.1f}" if cp["proposed_raw"] else "---"
        p_val = f"{test['p_value']:.3f}" if test["p_value"] is not None else "---"
        r_val = f"{test['effect_size_r']:.2f}" if test["effect_size_r"] is not None else "---"

        lines.append(
            f"{c['pattern']} & {c['rps']} & {bl_mean} & {bl_std} "
            f"& {pr_mean} & {pr_std} & {p_val} & {r_val} \\\\"
        )

    lines.extend(latex_table_footer())

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[STATS] LaTeX resource table saved to {output_path}")


def export_latex_fidelity_table(fidelity_summary: dict[str, Any], output_path: str) -> None:
    """Fidelity Guard 実験結果を LaTeX 表として出力する。"""
    ensure_parent_dir(output_path)

    lines = latex_table_header(
        caption="Fidelity Guard によるドリフト検知の先行性",
        label="tab:fidelity-guard",
        col_spec="lcccc",
        header_rows=[
            r"Scenario & Trials & Fidelity Leads & Rate (\%) & Verdict \\",
        ],
    )

    scenarios = fidelity_summary.get("scenarios", {})
    for name, data in scenarios.items():
        n = data.get("n_trials", 0)
        leads = data.get("fidelity_leads_count", 0)
        rate = data.get("fidelity_leads_rate", 0) * 100
        verdict = "Confirmed" if rate >= 66.7 else "Partial" if rate > 0 else "Not confirmed"

        lines.append(
            f"{name} & {n} & {leads}/{n} & {rate:.0f} & {verdict} \\\\"
        )

    lines.extend(latex_table_footer(notes=[
        r"Fidelity Leads: Fidelity スコアの崩壊が予測精度低下より先行した試行数",
    ]))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[STATS] Fidelity Guard LaTeX table saved to {output_path}")


def export_latex_confirming_detection_table(
    fidelity_summary: dict[str, Any], output_path: str,
) -> None:
    """Fidelity Guard 実験結果（確認指標としての検知性能）を LaTeX 表として出力する。

    shap_drift_score は精度低下の先行指標ではなく確認指標であることが実験で
    確定したため、confirming_detected / detection_lag を
    用いる。旧指標 fidelity_leads ベースの表は export_latex_fidelity_table に
    後方互換のため残し、本関数はそれを置き換えるものではなく追加の出力先とする。
    """
    ensure_parent_dir(output_path)

    lines = latex_table_header(
        caption="Fidelity Guard による概念ドリフト検知性能（確認指標）",
        label="tab:fidelity-guard-confirming",
        col_spec="lccccc",
        header_rows=[
            r"Scenario & Trials & Confirming Detected & Rate (\%)"
            r" & 95\% CI & Mean Lag (windows) \\",
        ],
    )

    scenarios = fidelity_summary.get("scenarios", {})
    for name, data in scenarios.items():
        n = data.get("n_trials", 0)
        detected = data.get("confirming_detected_count", 0)
        ci = wilson_score_interval(detected, n)
        rate_str = f"{ci['rate']:.1f}" if ci["rate"] is not None else "---"
        ci_str = f"[{ci['lower']:.1f}, {ci['upper']:.1f}]" if ci["lower"] is not None else "---"
        mean_lag = data.get("mean_detection_lag")
        lag_str = f"{mean_lag:.1f}" if mean_lag is not None else "---"

        lines.append(
            f"{name} & {n} & {detected}/{n} & {rate_str} & {ci_str} & {lag_str} \\\\"
        )

    lines.extend(latex_table_footer(notes=[
        r"Confirming Detected: 精度低下（MSEがbaseline平均の3倍超）に対し"
        r"shap\_drift\_scoreがMAX\_CONFIRMING\_LAG（8ウィンドウ）以内に検知した試行数",
        r"95\% CI: Wilson score interval",
        r"旧指標 fidelity\_leads（厳密な先行判定）は構造的にほぼ常に0\%になるため"
        r"本表には含めない（生データには後方互換のため保持）",
    ]))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[STATS] Fidelity Guard (confirming detection) LaTeX table saved to {output_path}")


def _compute_std(values: list[float]) -> float:
    if not values:
        return 0.0
    arr = np.array(values)
    return float(arr.std(ddof=1)) if len(arr) > 1 else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="論文用統計解析",
    )
    parser.add_argument("--batch-dir", required=True, help="バッチ結果ディレクトリ")
    parser.add_argument("--output-dir", default=None, help="出力ディレクトリ")

    args = parser.parse_args()

    if not os.path.isdir(args.batch_dir):
        print(f"[ERROR] ディレクトリが見つかりません: {args.batch_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir = args.output_dir or args.batch_dir
    os.makedirs(output_dir, exist_ok=True)

    print("[STATS] 統計解析開始...")
    results = analyze_batch_results(args.batch_dir)

    if not results:
        sys.exit(1)

    stats_path = os.path.join(output_dir, "summary_stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"[STATS] 統計結果を保存: {stats_path}")

    export_latex_loss_table(results, os.path.join(output_dir, "table_loss_rate.tex"))
    export_latex_resource_table(results, os.path.join(output_dir, "table_cpu_peak.tex"))

    print("[STATS] 統計解析完了")


if __name__ == "__main__":
    main()

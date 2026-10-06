# self-proving-observation/
# └── scripts/
#     └── pool_control_experiment_stats.py
#
# 対照実験（run_cron.sh）は週次のFidelity Guardと同じく複数回の夜間cron実行を
# 積み増して蓄積する設計だが、run単位の集計（stats.py analyze_batch_results）
# しか用意されておらず、run横断でプールした検定は行えなかった。
#の修正（verification_failed除外）を反映した上で、
# 複数のbatch_*ディレクトリ（＝各cron run）を横断してMann-Whitney U検定・
# 信頼区間を計算する一時的な再解析用スクリプト（対話セッションでの調査目的、
# 恒久的なCLIツールとしての整備は別途要検討）。

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_config import ensure_utf8_stdio
from src.measure._common import group_runs
from src.measure.aggregate import scan_batch_dir
from src.measure.stats import (
    _count_verification_failed,
    analyze_condition,
    bootstrap_ci,
    fisher_exact_loss_occurrence,
    mann_whitney_test,
    trend_test_by_pattern,
)

ensure_utf8_stdio()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="複数batch_*ディレクトリを横断して欠損率統計を再集計する",
    )
    parser.add_argument("batch_dirs", nargs="+", help="batch_* ディレクトリのパス（複数可）")
    parser.add_argument("--output", default="", help="結果JSONの保存先（省略時は標準出力のみ）")
    args = parser.parse_args()

    all_runs = []
    per_batch_counts = {}
    for bd in args.batch_dirs:
        runs = scan_batch_dir(bd)
        per_batch_counts[bd] = len(runs)
        all_runs.extend(runs)

    print(f"[POOL] {len(args.batch_dirs)}バッチ・計{len(all_runs)}試行を読み込み")
    for bd, n in per_batch_counts.items():
        print(f"  {bd}: {n}試行")

    groups = group_runs(all_runs)
    conditions = []
    for (pattern, rps), trials in sorted(groups.items()):
        entry = {"pattern": pattern, "rps": rps, "n_trials": len(trials)}

        baseline_vals = analyze_condition(trials, "baseline", "loss_rate_percent")
        proposed_vals = analyze_condition(trials, "proposed", "loss_rate_percent")
        test_result = mann_whitney_test(baseline_vals, proposed_vals)
        occurrence_result = fisher_exact_loss_occurrence(baseline_vals, proposed_vals)
        baseline_ci = bootstrap_ci(baseline_vals)
        proposed_ci = bootstrap_ci(proposed_vals)

        entry["loss_rate"] = {
            "test": test_result,
            "occurrence_test": occurrence_result,
            "baseline_ci": baseline_ci,
            "proposed_ci": proposed_ci,
            "baseline_raw": baseline_vals,
            "proposed_raw": proposed_vals,
            "n_verification_failed": {
                "baseline": _count_verification_failed(trials, "baseline"),
                "proposed": _count_verification_failed(trials, "proposed"),
            },
        }

        # CPU/メモリピーク使用率の統計比較。論文5.1が
        # 評価指標として掲げMann-Whitney U検定にかける対象と明記しているが、
        # loss_rate_percent以外はこれまで実際にこのスクリプトから計算されたことが
        # なかった（`analyze_condition`は元々metric_key引数を取る汎用実装のため
        # 呼び出しを追加するだけで済む、非破壊的な拡張）。
        for metric_key, label in (("cpu_peak", "cpu_peak"), ("mem_peak", "mem_peak")):
            b_vals = analyze_condition(trials, "baseline", metric_key)
            p_vals = analyze_condition(trials, "proposed", metric_key)
            entry[label] = {
                "test": mann_whitney_test(b_vals, p_vals),
                "baseline_ci": bootstrap_ci(b_vals),
                "proposed_ci": bootstrap_ci(p_vals),
                "n_baseline": len(b_vals),
                "n_proposed": len(p_vals),
            }

        conditions.append(entry)

    trend_by_pattern = trend_test_by_pattern(conditions)
    result = {
        "conditions": conditions,
        "total_trials": len(all_runs),
        "batches": per_batch_counts,
        "trend_by_pattern": trend_by_pattern,
    }

    print("\n=== プール後の欠損率統計（verification_failed除外済み） ===")
    for c in conditions:
        lr = c["loss_rate"]
        bm = lr["baseline_ci"]["mean"]
        pm = lr["proposed_ci"]["mean"]
        p = lr["test"]["p_value"]
        r = lr["test"]["effect_size_r"]
        occ = lr["occurrence_test"]
        occ_p = occ["p_value"]
        nvf = lr["n_verification_failed"]
        bm_s = f"{bm:.2f}" if bm is not None else "N/A"
        pm_s = f"{pm:.2f}" if pm is not None else "N/A"
        p_s = f"{p}" if p is not None else "N/A"
        occ_p_s = f"{occ_p}" if occ_p is not None else "N/A"
        sig = "  <-- MWU SIGNIFICANT" if p is not None and p < 0.05 else ""
        occ_sig = "  <-- OCCURRENCE SIGNIFICANT" if occ_p is not None and occ_p < 0.05 else ""
        occ_loss_s = f"b={occ['baseline_loss_count']},p={occ['proposed_loss_count']}"
        print(
            f"{c['pattern']:8s} rps={c['rps']:<6} n={c['n_trials']:<3} "
            f"baseline={bm_s:>8} proposed={pm_s:>8} p={p_s:<10} r={r} "
            f"occ_loss({occ_loss_s}) occ_p={occ_p_s:<10} "
            f"vf(b={nvf['baseline']},p={nvf['proposed']}){sig}{occ_sig}"
        )

    print("\n=== プール後のリソースピーク統計（CPU/メモリ、%） ===")
    for c in conditions:
        for metric_key, label in (("cpu_peak", "CPU"), ("mem_peak", "MEM")):
            m = c[metric_key]
            bm = m["baseline_ci"]["mean"]
            pm = m["proposed_ci"]["mean"]
            p = m["test"]["p_value"]
            r = m["test"]["effect_size_r"]
            bm_s = f"{bm:.1f}" if bm is not None else "N/A"
            pm_s = f"{pm:.1f}" if pm is not None else "N/A"
            p_s = f"{p}" if p is not None else "N/A"
            sig = "  <-- SIGNIFICANT" if p is not None and p < 0.05 else ""
            print(
                f"{c['pattern']:8s} rps={c['rps']:<6} {label:3s} "
                f"n=(b{m['n_baseline']},p{m['n_proposed']}) "
                f"baseline={bm_s:>8} proposed={pm_s:>8} p={p_s:<10} r={r}{sig}"
            )

    print("\n=== パターン別 用量反応傾向検定（Cochran-Armitage、baseline欠損発生率） ===")
    for pattern, t in sorted(trend_by_pattern.items()):
        if t["z_statistic"] is None:
            print(f"{pattern:8s} n_levels={t['n_levels']} (3水準未満のため未実施)")
            continue
        sig = "  <-- TREND SIGNIFICANT (increasing)" if (
            t["p_value_one_sided_increasing"] is not None
            and t["p_value_one_sided_increasing"] < 0.05
        ) else ""
        print(
            f"{pattern:8s} n_levels={t['n_levels']} z={t['z_statistic']} "
            f"p_two_sided={t['p_value_two_sided']} "
            f"p_one_sided_increasing={t['p_value_one_sided_increasing']}{sig}"
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n[POOL] 結果を保存: {args.output}")


if __name__ == "__main__":
    main()

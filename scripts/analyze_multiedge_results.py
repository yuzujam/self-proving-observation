# self-proving-observation/
# └── scripts/
#     └── analyze_multiedge_results.py
#
# scripts/run_multiedge_experiment.sh が生成する results/multiedge_* ディレクトリ群
# （複数のEDGE_COUNT・複数試行を横断して蓄積したもの）を集計し、
# 「仮想エッジ数(N)が増えるほど中央受付層の欠損発生率が用量反応的に悪化するか」を
# Cochran-Armitage傾向検定で評価する（doc/pipeline-spec.md「補強実験」節）。
# 既存のsrc/measure/stats.pyのcochran_armitage_trend_test（doc/decisions.md
# 2026-08-14で導入済み）をRPS軸ではなくエッジ数(N)軸で再利用する、対話セッション
# 向けの一時的な再解析スクリプト（pool_control_experiment_stats.pyと同種の位置づけ）。

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_config import ensure_utf8_stdio
from src.measure.stats import cochran_armitage_trend_test

ensure_utf8_stdio()


def load_trial(result_dir: Path) -> dict | None:
    """1つのresults/multiedge_*ディレクトリからメタデータと欠損率を読み込む。

    meta.json・loss_rate.jsonのいずれかが欠けている（実行途中で中断された等）
    場合はNoneを返し、呼び出し元でスキップできるようにする。
    """
    meta_path = result_dir / "meta.json"
    loss_path = result_dir / "loss_rate.json"
    if not meta_path.exists() or not loss_path.exists():
        return None

    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    with open(loss_path, encoding="utf-8") as f:
        loss = json.load(f)

    proposed = loss.get("proposed")
    if proposed is None:
        return None

    return {
        "dir": str(result_dir),
        "edge_count": meta["edge_count"],
        "edge_mode": meta["edge_mode"],
        "loss_rate_percent": proposed["loss_rate_percent"],
        "verification_failed": proposed.get("verification_failed", False),
    }


def group_by_mode_and_count(trials: list[dict]) -> dict[str, dict[int, list[dict]]]:
    """edge_mode → edge_count → 試行リスト、の2段グループ化。"""
    grouped: dict[str, dict[int, list[dict]]] = {}
    for t in trials:
        by_count = grouped.setdefault(t["edge_mode"], {})
        by_count.setdefault(t["edge_count"], []).append(t)
    return grouped


def trend_test_by_mode(trials: list[dict]) -> dict:
    """edge_mode（fixed-total / scaled）ごとに、エッジ数(N)を用量とした
    Cochran-Armitage傾向検定を行う。verification_failedな試行は分母から除外する
    （doc/known-limitations.md #Xと同じ扱い方針）。
    """
    grouped = group_by_mode_and_count(trials)
    result = {}
    for mode, by_count in grouped.items():
        levels: list[float] = []
        ns: list[int] = []
        xs: list[int] = []
        for edge_count in sorted(by_count):
            valid = [t for t in by_count[edge_count] if not t["verification_failed"]]
            n = len(valid)
            if n == 0:
                continue
            x = sum(1 for t in valid if t["loss_rate_percent"] > 0)
            levels.append(float(edge_count))
            ns.append(n)
            xs.append(x)
        result[mode] = {
            "edge_counts": [int(x) for x in levels],
            "n_trials_per_level": ns,
            "loss_occurrence_per_level": xs,
            "trend_test": cochran_armitage_trend_test(levels, ns, xs),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="複数のresults/multiedge_*ディレクトリを横断してN軸の傾向検定を行う",
    )
    parser.add_argument(
        "result_dirs", nargs="+", help="results/multiedge_* ディレクトリのパス（複数可）",
    )
    parser.add_argument("--output", default="", help="結果JSONの保存先（省略時は標準出力のみ）")
    args = parser.parse_args()

    trials = []
    skipped = 0
    for d in args.result_dirs:
        trial = load_trial(Path(d))
        if trial is None:
            skipped += 1
            continue
        trials.append(trial)

    print(f"[MULTIEDGE] {len(trials)}試行を読み込み（{skipped}件は不完全のためスキップ）")
    if not trials:
        print("[MULTIEDGE] 有効な試行が0件のため終了します。")
        return

    for t in trials:
        flag = " [UNVERIFIED]" if t["verification_failed"] else ""
        print(
            f"  N={t['edge_count']:>3d}  mode={t['edge_mode']:<12s}  "
            f"loss_rate={t['loss_rate_percent']:.4f}%{flag}  ({t['dir']})"
        )

    result = trend_test_by_mode(trials)

    print("\n=== 傾向検定結果（エッジ数Nに対する欠損発生率） ===")
    for mode, data in result.items():
        tt = data["trend_test"]
        print(f"  [{mode}] N水準: {data['edge_counts']}")
        print(
            f"    試行数/水準: {data['n_trials_per_level']}  "
            f"欠損発生数/水準: {data['loss_occurrence_per_level']}"
        )
        if tt["z_statistic"] is None:
            print(
                f"    水準数不足（{tt['n_levels']}水準）のため検定不可。"
                "N=1,2,4,8等、3水準以上のデータが必要。"
            )
        else:
            print(
                f"    z={tt['z_statistic']:.4f}  "
                f"p(two-sided)={tt['p_value_two_sided']:.4f}  "
                f"p(one-sided, N増加で悪化)={tt['p_value_one_sided_increasing']:.4f}"
            )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n[MULTIEDGE] 結果を保存しました: {args.output}")


if __name__ == "__main__":
    main()

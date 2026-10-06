# self-proving-observation/
# └── scripts/
#     └── render_resource_table.py
#
# 論文6.5節（リソース消費）の表を、resource_baseline.csv・
# resource_proposed.csv（monitor/resource.pyが対照実験の各trialごとに生成する
# docker統計CSV）から再現するためのスクリプト。
# 6.5節の指標は「時刻ごとに対象スタックの全コンテナの値を合計してから、その合計値の
# 系列全体に対して平均・中央値・p95を取る」もので、既存のparse_resource_csv()の
# 行単位peak/meanとは異なるため、src/measure/aggregate.pyのaggregate_resource_totals()
# を新設して算出する。対話セッション向けの一時的な再解析スクリプト
# （pool_control_experiment_stats.pyと同種の位置づけ）。

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_config import ensure_utf8_stdio
from src.measure.aggregate import aggregate_resource_totals

ensure_utf8_stdio()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="対照実験のresource_*.csvから6.5節形式のリソース消費表を再現する",
    )
    parser.add_argument(
        "--baseline-csv", nargs="+", required=True,
        help="baseline側のresource_baseline.csv（複数バッチ・複数試行分、複数可）",
    )
    parser.add_argument(
        "--proposed-csv", nargs="+", required=True,
        help="proposed側のresource_proposed.csv（複数バッチ・複数試行分、複数可）",
    )
    parser.add_argument("--output", default="", help="結果JSONの保存先（省略時は標準出力のみ）")
    args = parser.parse_args()

    baseline = aggregate_resource_totals(args.baseline_csv, container_prefix="baseline-")
    proposed = aggregate_resource_totals(args.proposed_csv, container_prefix="proposed-")

    print(f"[RESOURCE] baseline: {len(args.baseline_csv)}ファイル、"
          f"{baseline['n_timestamps']}時刻分を集計")
    print(f"[RESOURCE] proposed: {len(args.proposed_csv)}ファイル、"
          f"{proposed['n_timestamps']}時刻分を集計")

    print("\n| 指標 | baseline | proposed |")
    print("|---|---|---|")

    def _fmt_pct(v: float | None) -> str:
        return f"{v:.1f}%" if v is not None else "N/A"

    def _fmt_mib(v: float | None) -> str:
        return f"{v:,.1f} MiB" if v is not None else "N/A"

    bc, pc = baseline["cpu_percent_total"], proposed["cpu_percent_total"]
    bm, pm = baseline["mem_mib_total"], proposed["mem_mib_total"]
    print(
        f"| CPU使用率（全コンテナ合計%、平均） | "
        f"{_fmt_pct(bc['mean'])} | {_fmt_pct(pc['mean'])} |"
    )
    print(f"| CPU使用率（同、中央値） | {_fmt_pct(bc['median'])} | {_fmt_pct(pc['median'])} |")
    print(f"| CPU使用率（同、p95） | {_fmt_pct(bc['p95'])} | {_fmt_pct(pc['p95'])} |")
    print(
        f"| メモリ使用量（全コンテナ合計、平均） | "
        f"{_fmt_mib(bm['mean'])} | {_fmt_mib(pm['mean'])} |"
    )
    print(f"| メモリ使用量（同、中央値） | {_fmt_mib(bm['median'])} | {_fmt_mib(pm['median'])} |")
    print(f"| メモリ使用量（同、p95） | {_fmt_mib(bm['p95'])} | {_fmt_mib(pm['p95'])} |")

    if args.output:
        result = {"baseline": baseline, "proposed": proposed}
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n[RESOURCE] 結果を保存しました: {args.output}")


if __name__ == "__main__":
    main()

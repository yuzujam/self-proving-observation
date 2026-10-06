# self-proving-observation/
# └── src/
#     └── measure/
#         └── _common.py  — measure/visualize 間で重複していた補助関数の集約

import os
from collections import defaultdict
from typing import Any


def ensure_parent_dir(path: str) -> None:
    """出力先ファイルの親ディレクトリを作成する（存在すれば何もしない）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def group_runs(
    runs: list[dict[str, Any]],
) -> dict[tuple[Any, Any], list[dict[str, Any]]]:
    """(pattern, rps) をキーに試行結果をグループ化する。"""
    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        key = (run["pattern"], run["rps"])
        groups[key].append(run)
    return groups


def latex_table_header(
    caption: str,
    label: str,
    col_spec: str,
    header_rows: list[str],
) -> list[str]:
    """LaTeX table の先頭ブロック（\\begin{table} 〜 \\midrule）を生成する。"""
    return [
        r"\begin{table}[htbp]",
        r"\centering",
        rf"\caption{{{caption}}}",
        rf"\label{{{label}}}",
        rf"\begin{{tabular}}{{{col_spec}}}",
        r"\toprule",
        *header_rows,
        r"\midrule",
    ]


def latex_table_footer(notes: list[str] | None = None) -> list[str]:
    """LaTeX table の末尾ブロック（\\bottomrule 〜 \\end{table}）を生成する。"""
    lines = [r"\bottomrule", r"\end{tabular}"]
    if notes:
        lines.append(r"\begin{tablenotes}")
        lines.append(r"\small")
        lines.extend(rf"\item {note}" for note in notes)
        lines.append(r"\end{tablenotes}")
    lines.append(r"\end{table}")
    return lines

# self-proving-observation/
# └── tests/
#     └── test_ablation_env_export.py
#
# run_ablation_experiment.sh が
# docker-compose.ablation.yml の必須変数 ABLATION_DATA_DIR を、個別の
# `docker compose ...` 呼び出しへ前置する形でしか渡しておらず、シェル自体に
# export していなかった。そのため同じスクリプトがバックグラウンドジョブとして
# 起動する src/monitor/resource.py（内部で独自に `docker compose ps -q` を
# 実行する）には ABLATION_DATA_DIR が伝播せず、`error while interpolating
# ... required variable ABLATION_DATA_DIR is missing a value` で失敗し、
# 常に空のコンテナID一覧を返していた。collect_docker_stats() の
# 「コンテナが無ければ空リストを返す」フォールバックが、この設定ミスによる
# 失敗と「Docker未インストール等の正当な理由」を区別なく同じ空リストとして
# 扱うため、導入以来ホスト全体の値へサイレントにフォールバックし続けていた。
#
# resource.py を起動する行より前に export ABLATION_DATA_DIR= があることを
# 機械的に確認し、同種の見落とし（export忘れ・順序の逆転）が再発しないことを
# 検証する。

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_ablation_experiment.sh"

_EXPORT_RE = re.compile(r"^\s*export\s+ABLATION_DATA_DIR=")
# コメント中の言及（バグの経緯説明等）ではなく、実際の起動コマンド行のみを
# 対象にする（`python3 ... resource.py` のような呼び出し行）。
_RESOURCE_PY_INVOCATION_RE = re.compile(r"resource\.py\b")


def _is_comment(line: str) -> bool:
    return line.strip().startswith("#")


def test_ablation_data_dir_is_exported_before_resource_py_is_launched():
    assert SCRIPT_PATH.exists(), f"{SCRIPT_PATH} が見つかりません"

    lines = SCRIPT_PATH.read_text(encoding="utf-8").splitlines()

    export_line_no = next(
        (i for i, line in enumerate(lines) if _EXPORT_RE.match(line)), None
    )
    resource_py_line_no = next(
        (
            i
            for i, line in enumerate(lines)
            if _RESOURCE_PY_INVOCATION_RE.search(line) and not _is_comment(line)
        ),
        None,
    )

    assert export_line_no is not None, (
        "run_ablation_experiment.sh に `export ABLATION_DATA_DIR=` が"
        "見つかりません。個別コマンドへの"
        "前置だけでは、バックグラウンド起動されるresource.pyへ環境変数が"
        "伝播しません。"
    )
    assert resource_py_line_no is not None, (
        "run_ablation_experiment.sh が src/monitor/resource.py を起動する"
        "行が見つかりません（想定していたリソース計測の起動経路が変わった"
        "可能性があります。本テストの前提を見直してください）。"
    )
    assert export_line_no < resource_py_line_no, (
        "`export ABLATION_DATA_DIR=` がresource.py起動より後ろにあります。"
        "resource.pyは独自に"
        "`docker compose ps -q` を実行するため、起動時点でこの環境変数が"
        "既にexport済みでなければコンテナ検出に失敗し、ホスト全体の値へ"
        "サイレントにフォールバックします。"
    )

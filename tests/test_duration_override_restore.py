# self-proving-observation/
# └── tests/
#     └── test_duration_override_restore.py
#
# doc/known-limitations.md #HH: experiment.envが`export DURATION=120`等を設定して
# いる場合、load_experiment_env（scripts/lib/common.sh）のsourceが呼び出し元指定の
# DURATIONを無条件で上書きしてしまう。run_ablation_experiment.shの実機実行で実害が
# 発生し（意図の4倍の時間で完走）、run_ablation_batch.sh・run_multiedge_batch.sh・
# run_multiedge_experiment.shにも同型の問題が再発したため、全4スクリプトに
# 「呼び出し元の値をload_experiment_env呼び出し前に退避し、呼び出し後に優先的に
# 復元する」パターンを実装した（doc/decisions.md該当箇所参照）。
#
# 同型の問題が静かに再発しないよう、既知の4スクリプトにこの退避・復元パターンが
# 維持されていることを機械的に確認する。

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# doc/known-limitations.md #HHで実際にDURATION上書きの実害・再発が確認された
# スクリプト。新たに同型の問題を持つスクリプトが追加された場合はここにも追記する。
SCRIPTS_REQUIRING_RESTORE_PATTERN = [
    "run_ablation_batch.sh",
    "run_ablation_experiment.sh",
    "run_multiedge_batch.sh",
    "run_multiedge_experiment.sh",
]


def test_duration_override_restore_pattern_present():
    offenders = []
    for name in SCRIPTS_REQUIRING_RESTORE_PATTERN:
        path = SCRIPTS_DIR / name
        assert path.exists(), f"{path} が見つかりません"
        lines = path.read_text(encoding="utf-8").splitlines()

        capture_idx = next(
            (i for i, line in enumerate(lines) if '_CALLER_DURATION="${DURATION:-}"' in line),
            None,
        )
        load_idx = next(
            (i for i, line in enumerate(lines) if line.strip() == "load_experiment_env"),
            None,
        )
        restore_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if "_CALLER_DURATION" in line and "if" in line
            ),
            None,
        )

        if capture_idx is None or load_idx is None or restore_idx is None:
            offenders.append(name)
            continue
        if not (capture_idx < load_idx < restore_idx):
            offenders.append(name)

    assert not offenders, (
        "load_experiment_env呼び出し前後でDURATIONを退避・復元するパターン"
        "（doc/known-limitations.md #HH）が欠落、または順序が崩れています: "
        f"{offenders}"
    )

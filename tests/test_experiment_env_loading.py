# self-proving-observation/
# └── tests/
#     └── test_experiment_env_loading.py
#
# doc/known-limitations.md #CCC: backup_clickhouse.sh・rotate_and_backup.sh だけが
# experiment.env を素の`source`で読んでおり、共通の load_experiment_env（`set -a`で
# 自動export＋OBS_VENV対応）を使っていなかった。素の`source`では、experiment.envが
# `export`なしで書かれていた場合に CLICKHOUSE_USER/PASSWORD が子プロセス
# （record_backup_result.py）へ伝わらず、backup_logへの記録が認証失敗でサイレントに
# 全滅する（#GG・#II、baseline-node側の更新漏れで実際に数日間発生）。
#
# (1) load_experiment_env が`export`なしの変数も子プロセスへ渡すこと
# (2) 上記2スクリプトが素の`source`ではなくこの関数を使うこと
# を検証する。

import pathlib
import re
import subprocess
import textwrap

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def test_load_experiment_env_exports_variables_written_without_export(tmp_path):
    scripts = tmp_path / "scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "lib" / "common.sh").write_text(
        (SCRIPTS_DIR / "lib" / "common.sh").read_text(encoding="utf-8"), encoding="utf-8",
    )
    # `export`を付けずに書かれたexperiment.env（付け忘れを想定）
    (scripts / "experiment.env").write_text(
        "EXPERIMENT_ENV_TEST_VAR=hello\nCLICKHOUSE_USER=default\n", encoding="utf-8",
    )
    harness = tmp_path / "harness.sh"
    harness.write_text(
        textwrap.dedent(f"""
            SCRIPT_DIR="{scripts.as_posix()}"
            source "$SCRIPT_DIR/lib/common.sh"
            load_experiment_env
            # 子プロセスから見えるのはexportされた変数だけ
            env | grep '^EXPERIMENT_ENV_TEST_VAR=' || echo "NOT-EXPORTED"
        """),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", harness.as_posix()], capture_output=True, encoding="utf-8", timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "EXPERIMENT_ENV_TEST_VAR=hello"


def test_backup_scripts_use_load_experiment_env_not_a_plain_source():
    plain_source = re.compile(r"^\s*source\s+.*experiment\.env", re.MULTILINE)
    for name in ("backup_clickhouse.sh", "rotate_and_backup.sh"):
        text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert "load_experiment_env" in code, f"{name}: load_experiment_env を呼んでいません"
        assert not plain_source.search(code), (
            f"{name}: experiment.env を素の`source`で読んでいます（#CCC）。"
            "load_experiment_env を使ってください。"
        )

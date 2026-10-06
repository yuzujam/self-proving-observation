# self-proving-observation/
# └── tests/
#     └── test_rotate_extract_pipeline.py
#
# rotate_and_backup.sh は「ESインデックス → 1分集計CSV(gzip) → S3-compatible object storage → ESから削除」
# の順で処理する。以前は `python3 extract_features.py | gzip > f && [[ -s f ]]` と
# 書かれており、パイプラインの終了ステータスが末尾のgzipのものになる（pipefail
# なし）ため、extract_features.py の失敗（ES障害等）がマスクされていた。gzipは
# 空入力でも20バイトの有効なファイルを作るので `-s` も通り、「抽出失敗→空の
# バックアップをアップロード→ESインデックスを不可逆に削除」という経路が成立して
# いた。
#
# 修正後の extract_index_to_gz() が、抽出失敗・空出力・ヘッダのみの出力を必ず
# 失敗として扱うことを、実ES・実Pythonスクリプトに依存せず（偽python3で）検証する。

import pathlib
import subprocess
import textwrap

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROTATE_SCRIPT = REPO_ROOT / "scripts" / "rotate_and_backup.sh"


def _msys_path(path: pathlib.Path) -> str:
    # Windows上のGit Bash(MSYS)ではPATHへ"C:/..."形式を連結すると":"が区切りと
    # 誤解釈される。PATHに足すエントリだけ"/c/..."形式へ変換する。
    s = path.as_posix()
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _extract_function(name: str) -> str:
    text = ROTATE_SCRIPT.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end_marker = "\n}\n"
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


def _run(tmp_path: pathlib.Path, fake_python3_body: str) -> tuple[int, pathlib.Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python3"
    fake_python.write_text("#!/bin/bash\n" + fake_python3_body, encoding="utf-8")
    subprocess.run(
        ["bash", "-c", f'chmod +x "{fake_python.as_posix()}"'], check=True, timeout=10
    )

    out = tmp_path / "features.csv.gz"
    script = textwrap.dedent(f"""
        set -uo pipefail
        export PATH="{_msys_path(bin_dir)}:$PATH"
        SCRIPT_DIR="{tmp_path.as_posix()}"
        LOG_FILE="{tmp_path.as_posix()}/log.txt"

        {_extract_function("extract_index_to_gz")}

        extract_index_to_gz "logstash-2026.01.01" "{out.as_posix()}"
    """)
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, encoding="utf-8", timeout=15
    )
    return result.returncode, out


def test_extract_failure_with_empty_stdout_is_a_failure(tmp_path):
    # ES障害: extract_features.pyは何も出力せず非0で終了する。
    # 旧実装ではgzipが20バイトの空gzipを作り成功扱いになっていた。
    code, _ = _run(tmp_path, "exit 1\n")
    assert code != 0


def test_extract_failure_after_partial_output_is_a_failure(tmp_path):
    # 途中まで出力してから異常終了した場合も、切り詰められたCSVを成功扱いに
    # しない（ヘッダ+行が揃っていても、抽出プロセス自体が失敗していれば失敗）。
    code, _ = _run(tmp_path, "echo header\necho row1\nexit 1\n")
    assert code != 0


def test_zero_exit_with_empty_output_is_a_failure(tmp_path):
    code, _ = _run(tmp_path, "exit 0\n")
    assert code != 0


def test_header_only_output_is_a_failure(tmp_path):
    # ヘッダ行のみ（データ行なし）は「1分窓が1件も無い」抽出であり、
    # このバックアップを根拠にESインデックスを削除してはならない。
    code, _ = _run(tmp_path, "echo window_start,event_count\nexit 0\n")
    assert code != 0


def test_header_and_rows_succeed(tmp_path):
    code, out = _run(
        tmp_path,
        "echo window_start,event_count\necho 2026-01-01 00:00,5\nexit 0\n",
    )
    assert code == 0
    assert out.exists() and out.stat().st_size > 20

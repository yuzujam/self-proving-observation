# self-proving-observation/
# └── tests/
#     └── test_backup_clickhouse_count_query_failure.py
#
# backup_clickhouse.shのbackup_tableは
# 件数取得クエリが失敗しても`rows=0`にフォールバックしており、「バックアップ
# 成功・0件」と誤記録され、prune安全条件（S3-compatible object storage整合性確認
# 済みの日付のみローカルthreat_eventsを削除）をすり抜けうる状態だった。
# 修正後は「件数クエリ自体の失敗（rowsを空文字のまま保持）」と「クエリは
# 成功し真に0件（rows="0"）」を区別する。この区別が将来のリファクタで
# 再び失われていないかを、実ClickHouse・実rcloneに依存せず検証する。

import pathlib
import subprocess
import textwrap

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup_clickhouse.sh"


def _msys_path(path: pathlib.Path) -> str:
    # Windows上のGit Bash(MSYS)では、PATH変数（":"区切り）に"C:/..."形式の
    # ドライブレターパスをそのまま連結すると、ドライブレター直後の":"が
    # フィールド区切りとして誤って解釈され、そのエントリが探索されない。
    # PATHに追加するエントリだけ"/c/..."形式(MSYSスタイル)に変換する
    # （source・リダイレクト先など単体のパス引数はas_posix()のままで動く）。
    s = path.as_posix()
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _extract_backup_table_function() -> str:
    text = BACKUP_SCRIPT.read_text(encoding="utf-8")
    start = text.index("backup_table() {")
    end_marker = "\n}\n"
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


def _run_backup_table(tmp_path: pathlib.Path, fake_curl_script: str) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(fake_curl_script, encoding="utf-8")
    # Windows(NTFS)ではPython側のos.chmodが実行ビットとしてbashに認識されない
    # ことがあるため、bash自身のchmodで確実に実行可能にする。
    subprocess.run(
        ["bash", "-c", f'chmod +x "{fake_curl.as_posix()}"'], check=True, timeout=10
    )

    backup_tmp = tmp_path / "backuptmp"
    backup_tmp.mkdir()

    # record_result()（本来はrecord_backup_result.pyをpython3で呼ぶ）を
    # 素通しの偽関数に差し替え、backup_table()が何を渡そうとしたかだけを
    # 標準出力に記録する。これにより実ClickHouse・実Pythonに依存しない。
    script = textwrap.dedent(f"""
        set -uo pipefail
        export PATH="{_msys_path(bin_dir)}:$PATH"
        LOG_FILE="{tmp_path.as_posix()}/log.txt"
        FAILURES=()
        CH_AUTH_ARGS=()
        CLICKHOUSE_URL="http://fake-clickhouse:8123"
        BACKUP_DATE="2026-01-01"
        BACKUP_TMP="{backup_tmp.as_posix()}"
        log() {{ :; }}
        record_result() {{
            echo "RECORD table=$1 status=$2 rows=${{3:-0}} err=${{4:-}}"
        }}

        {_extract_backup_table_function()}

        backup_table "threat_events" "timestamp"
    """)
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, encoding="utf-8", timeout=15
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    return result.stdout


# 両方とも FORMAT Parquet（エクスポートクエリ）は失敗させ、
# backup_table()の「件数取得も失敗」判定分岐（else側）を通す。
_COUNT_QUERY_FAILS = textwrap.dedent(
    """\
    #!/bin/bash
    args="$*"
    [[ "$args" == *"FORMAT Parquet"* ]] && exit 1
    [[ "$args" == *"SELECT count()"* ]] && exit 1
    exit 1
    """
)

_COUNT_QUERY_SUCCEEDS_WITH_TRUE_ZERO = textwrap.dedent(
    """\
    #!/bin/bash
    args="$*"
    [[ "$args" == *"FORMAT Parquet"* ]] && exit 1
    if [[ "$args" == *"SELECT count()"* ]]; then
        echo "0"
        exit 0
    fi
    exit 1
    """
)


def test_count_query_failure_is_not_recorded_as_success(tmp_path):
    output = _run_backup_table(tmp_path, _COUNT_QUERY_FAILS)
    assert "status=export_failed" in output, (
        "件数取得クエリ自体が失敗しているのに success として記録されています。"
        f"output={output!r}"
    )
    assert "status=success" not in output


def test_true_zero_rows_is_still_recorded_as_success(tmp_path):
    # 件数クエリが成功し本当に0件だったケースまで export_failed 扱いに
    # なってしまう過剰検知がないことを確認する。
    output = _run_backup_table(tmp_path, _COUNT_QUERY_SUCCEEDS_WITH_TRUE_ZERO)
    assert "status=success rows=0" in output, output

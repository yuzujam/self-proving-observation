# self-proving-observation/
# └── tests/
#     └── test_backup_prune_reconciliation.py
#
# backup_clickhouse.sh の日次バックアップは、UTC日が終わった
# 直後（00:00 UTC = 02:00 CEST）に前日分を対象に走る。Vectorのreduce（アイドル待ち）・
# バッチ・ワーカーの遅延で、日末の数秒分の行はバックアップ後に挿入されうる。ところが
# prune_verified_threat_events（7日以上前の日付をローカルから間引く）は
# rclone checkの結果しか見ず、「バックアップ時の件数」と「現在のローカル件数」を照合して
# いなかったため、バックアップに入っていない後着行までローカルから消えうる状態だった。
#
# 修正後の契約（いずれも「削除条件を厳しくする」方向のみ）:
#   - ローカル件数 > バックアップ記録件数 なら、その日を再エクスポートして件数を揃え、
#     揃った場合のみ削除する（揃わなければ削除せず保持し、FAILURESに積む）。
#   - 直近14日でsuccess記録の無い日（export_failed等）は日次実行で再バックアップする。
#
# 実ClickHouse・実rcloneには依存せず、リクエストを記録する偽curl・偽rcloneで検証する。

import pathlib
import subprocess
import textwrap

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup_clickhouse.sh"

FAKE_CURL = r"""#!/bin/bash
data=""; out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --data) data="$2"; shift 2 ;;
        -o) out="$2"; shift 2 ;;
        *) shift ;;
    esac
done
echo "$data" >> "$FAKE_DIR/requests.log"
d=$(sed -nE "s/.*= '([0-9]{4}-[0-9]{2}-[0-9]{2})'.*/\1/p" <<< "$data" | head -1)
t=$(sed -nE "s/.*table_name = '([a-z_]+)'.*/\1/p" <<< "$data" | head -1)
case "$data" in
    *"FROM backup_log AS bl"*)
        [ -f "$FAKE_DIR/clickhouse_down" ] && exit 22
        cat "$FAKE_DIR/candidates" 2>/dev/null ;;
    *"argMax(row_count"*)
        cat "$FAKE_DIR/backed_$d" 2>/dev/null || echo 0 ;;
    *"argMax(status"*)
        [ -f "$FAKE_DIR/clickhouse_down" ] && exit 22
        cat "$FAKE_DIR/ok_dates_$t" 2>/dev/null ;;
    *"ALTER TABLE"*)
        echo "$d" >> "$FAKE_DIR/mutations.log" ;;
    *"FORMAT Parquet"*)
        [ -f "$FAKE_DIR/export_fail" ] && exit 22
        echo "$d $data" | sed -E 's/ SELECT.*FROM ([a-z_]+) .*/ \1/' >> "$FAKE_DIR/exports.log"
        [ -n "$out" ] && echo parquet > "$out" ;;
    *"SELECT count()"*)
        cat "$FAKE_DIR/count_$d" 2>/dev/null || cat "$FAKE_DIR/count" ;;
esac
exit 0
"""

FAKE_RCLONE = """#!/bin/bash
exit 0
"""

FAKE_PYTHON3 = """#!/bin/bash
echo "$@" >> "$FAKE_DIR/python.log"
exit 0
"""


def _msys_path(path: pathlib.Path) -> str:
    s = path.as_posix()
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _function(name: str) -> str:
    text = BACKUP_SCRIPT.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end_marker = "\n}\n"
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


def _run(tmp_path, call, *, files=None, backup_date_from_env="", extra_setup=""):
    """call（bash）を、必要な関数を読み込んだ環境で実行し、(stdout, FAKE_DIR)を返す。"""
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("rclone", FAKE_RCLONE), ("python3", FAKE_PYTHON3)):
        f = bin_dir / name
        f.write_text(body, encoding="utf-8")
        subprocess.run(["bash", "-c", f'chmod +x "{f.as_posix()}"'], check=True, timeout=10)
    for name, content in (files or {}).items():
        (fake_dir / name).write_text(content, encoding="utf-8")

    backup_tmp = tmp_path / "obs-ch-backup-2026-09-20"
    backup_tmp.mkdir()

    script = textwrap.dedent(f"""
        set -uo pipefail
        export PATH="{_msys_path(bin_dir)}:$PATH"
        export FAKE_DIR="{fake_dir.as_posix()}"
        SCRIPT_DIR="{REPO_ROOT.as_posix()}/scripts"
        LOG_FILE="{tmp_path.as_posix()}/log.txt"
        FAILURES=()
        CH_AUTH_ARGS=()
        CLICKHOUSE_URL="http://fake-clickhouse:8123"
        REMOTE="remote"; BUCKET="bucket"; NODE_ID="proposed"
        BACKUP_DATE="2026-09-20"
        BACKUP_TMP="{backup_tmp.as_posix()}"
        _BACKUP_DATE_FROM_ENV="{backup_date_from_env}"
        log() {{ echo "$*" >> "$FAKE_DIR/script.log"; }}
        # 本物のrecord_resultはrecord_backup_result.pyを呼ぶ。ここでは記録内容を残し、
        # success時はbackup_logの「最新success件数」を模したファイルを更新する。
        record_result() {{
            local bdate="${{5:-$BACKUP_DATE}}"
            echo "$1 $2 rows=${{3:-0}} date=$bdate" >> "$FAKE_DIR/records.log"
            [[ "$2" == "success" ]] && echo "${{3:-0}}" > "$FAKE_DIR/backed_$bdate"
            return 0
        }}

        {_function("backup_table")}
        {_function("count_threat_events_on")}
        {_function("latest_backed_rows")}
        {_function("prune_verified_threat_events")}
        {_function("catch_up_failed_backups")}
        {extra_setup}

        {call}
        printf 'FAILURES=%s\\n' "${{#FAILURES[@]}}"
    """)
    # `bash -c <日本語を含むスクリプト>`はWindowsのコマンドライン変換で文字化けし構文が壊れうる
    # ため、UTF-8のファイルに書き出して実行する。
    harness = tmp_path / "harness.sh"
    harness.write_text(script, encoding="utf-8")
    # 偽curl・偽rcloneを1回ずつbashで起動するため、Windows（Git Bash）ではプロセス起動が
    # 遅く、上限7日×複数表のテストが単体でも約21秒、負荷時は30秒を超えた（2026-09-25、
    # 全件実行で1度だけTimeoutExpired）。ハング検知の上限として十分な余裕を持たせる。
    result = subprocess.run(
        ["bash", harness.as_posix()], capture_output=True, encoding="utf-8", timeout=120
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    return result.stdout, fake_dir


def _lines(fake_dir, name):
    f = fake_dir / name
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


# ---------------------------------------------------------------- prune

D = "2026-07-05"


def test_prune_deletes_when_local_count_equals_backup_count(tmp_path):
    out, fake = _run(
        tmp_path, "prune_verified_threat_events",
        files={"candidates": f"{D}\t1000\n", "count": "1000\n"},
    )
    assert _lines(fake, "mutations.log") == [D]
    assert _lines(fake, "exports.log") == []
    assert "FAILURES=0" in out


def test_prune_reexports_late_arrivals_then_deletes_once_counts_match(tmp_path):
    # バックアップ後に30行が後着し、ローカルは1030行。バックアップ記録は1000行。
    out, fake = _run(
        tmp_path, "prune_verified_threat_events",
        files={"candidates": f"{D}\t1000\n", "count": "1030\n", f"backed_{D}": "1000\n"},
    )
    exports = _lines(fake, "exports.log")
    assert len(exports) == 1 and exports[0].startswith(D) and exports[0].endswith("threat_events")
    assert f"threat_events success rows=1030 date={D}" in _lines(fake, "records.log")
    assert _lines(fake, "mutations.log") == [D]
    assert "FAILURES=0" in out


def test_prune_keeps_local_rows_when_reexport_fails(tmp_path):
    out, fake = _run(
        tmp_path, "prune_verified_threat_events",
        files={
            "candidates": f"{D}\t1000\n", "count": "1030\n", f"backed_{D}": "1000\n",
            "export_fail": "",
        },
    )
    assert _lines(fake, "mutations.log") == []  # 後着行がバックアップに無いまま消してはならない
    assert "FAILURES=0" not in out


def test_prune_allows_when_local_count_is_lower_than_backup(tmp_path):
    # ローカルがバックアップ以下（以前の間引きが途中まで進んだ等）なら、バックアップが
    # 全行を含むため削除して問題ない。再エクスポートも不要。
    _out, fake = _run(
        tmp_path, "prune_verified_threat_events",
        files={"candidates": f"{D}\t1000\n", "count": "500\n"},
    )
    assert _lines(fake, "mutations.log") == [D]
    assert _lines(fake, "exports.log") == []


def test_prune_skips_days_with_no_local_rows(tmp_path):
    _out, fake = _run(
        tmp_path, "prune_verified_threat_events",
        files={"candidates": f"{D}\t1000\n", "count": "0\n"},
    )
    assert _lines(fake, "mutations.log") == []
    assert _lines(fake, "exports.log") == []


def test_prune_does_nothing_when_no_candidates(tmp_path):
    _out, fake = _run(tmp_path, "prune_verified_threat_events", files={"candidates": ""})
    assert _lines(fake, "mutations.log") == []


# ------------------------------------------------------------- catch-up

CATCH_UP_CALL = "catch_up_failed_backups"


def _dates_back(first: int, last: int) -> list[str]:
    from datetime import date, timedelta

    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(first, last + 1)]


def test_catch_up_reexports_days_without_a_success_record(tmp_path):
    # 直近14日（2〜14日前）のうち、success記録が無い2日だけを再バックアップする。
    all_days = _dates_back(2, 14)
    missing = {all_days[3], all_days[7]}
    ok = "\n".join(d for d in all_days if d not in missing) + "\n"
    files = {
        "ok_dates_threat_events": ok,
        "ok_dates_pipeline_health": "\n".join(all_days) + "\n",
        "ok_dates_heartbeats": "\n".join(all_days) + "\n",
        "count": "10\n",
    }
    _out, fake = _run(tmp_path, CATCH_UP_CALL, files=files)

    exports = _lines(fake, "exports.log")
    assert sorted(e.split()[0] for e in exports) == sorted(missing)
    assert all(e.endswith("threat_events") for e in exports)
    assert {r for r in _lines(fake, "records.log")} == {
        f"threat_events success rows=10 date={d}" for d in missing
    }


def test_catch_up_does_nothing_when_clickhouse_is_unreachable(tmp_path):
    # 応答が無いのを「success記録が無い」と誤認して全日を再バックアップしない。
    _out, fake = _run(tmp_path, CATCH_UP_CALL, files={"clickhouse_down": "", "count": "10\n"})
    assert _lines(fake, "exports.log") == []


def test_catch_up_is_disabled_for_manually_specified_backup_date(tmp_path):
    _out, fake = _run(
        tmp_path, CATCH_UP_CALL, backup_date_from_env="2026-07-05", files={"count": "10\n"},
    )
    assert _lines(fake, "requests.log") == []


def test_catch_up_is_capped_per_table_per_run(tmp_path):
    # success記録が全く無い（13日分すべて欠落）場合でも、1回の実行で再バックアップするのは
    # 1表あたり最大7日まで（一斉エクスポートによる負荷の集中を避ける）。
    files = {
        "ok_dates_threat_events": "",
        "ok_dates_pipeline_health": "\n".join(_dates_back(2, 14)) + "\n",
        "ok_dates_heartbeats": "\n".join(_dates_back(2, 14)) + "\n",
        "count": "10\n",
    }
    _out, fake = _run(tmp_path, CATCH_UP_CALL, files=files)
    assert len(_lines(fake, "exports.log")) == 7


# -------------------------------------------------- stale leftover removal


def test_successful_export_removes_stale_leftover_for_same_table_and_date(tmp_path):
    # 前回upload_failedで/tmpに残った古いParquetを、新しい（完全な）エクスポートの後に
    # リトライ処理が再アップロードして上書きしてしまわないよう、成功時に消す。
    stale_dir = tmp_path / f"obs-ch-backup-{D}"
    stale_dir.mkdir()
    stale = stale_dir / f"threat_events_{D}.parquet"
    stale.write_text("old", encoding="utf-8")

    _out, fake = _run(
        tmp_path, f'backup_table "threat_events" "timestamp" "{D}"', files={"count": "10\n"},
    )

    assert _lines(fake, "records.log") == [f"threat_events success rows=10 date={D}"]
    assert not stale.exists()

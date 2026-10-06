# self-proving-observation/
# └── tests/
#     └── test_record_backup_result.py
#
# doc/known-limitations.md #FF: record_backup_result.py の main() は
# backup_log への INSERT が失敗しても例外を握りつぶし常に exit 0 で戻って
# おり、呼び出し元（backup_clickhouse.sh・rotate_and_backup.sh、計3箇所）が
# 戻り値経由で失敗を検知できなかった（backup_log自体が自己証明型完全性
# 保証の監査台帳であるため、#9が総括する「検証メカニズム自体がサイレント
# に壊れる」構造的パターンの一つ）。修正後は _record() がbool、main()が
# 失敗時にsys.exit(1)を返す契約になっている。この契約が将来のリファクタ
# で再び失われていないかを、実ClickHouseに依存せず検証する。

import argparse
import importlib.util
import pathlib
import sys
from unittest.mock import patch

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "record_backup_result.py"

_spec = importlib.util.spec_from_file_location("record_backup_result", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
record_backup_result = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(record_backup_result)


def _make_args(**overrides):
    defaults = dict(
        table="threat_events",
        status="success",
        rows=0,
        error="",
        backup_date="2026-01-01",
        node_id="proposed",
        clickhouse_url="http://fake-clickhouse:8123",
        quiet=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


_MAIN_ARGV = [
    "record_backup_result.py",
    "--table", "threat_events",
    "--status", "success",
    "--backup-date", "2026-01-01",
    "--node-id", "proposed",
    "--clickhouse-url", "http://fake-clickhouse:8123",
]


class TestRecordReturnsBool:
    def test_returns_false_when_insert_fails(self):
        args = _make_args()
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            assert record_backup_result._record(args) is False

    def test_returns_true_when_insert_succeeds(self):
        args = _make_args()
        with patch("urllib.request.urlopen"):
            assert record_backup_result._record(args) is True


class TestMainSurfacesFailure:
    def test_main_exits_nonzero_on_insert_failure(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", _MAIN_ARGV)
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            with pytest.raises(SystemExit) as exc_info:
                record_backup_result.main()
        assert exc_info.value.code == 1, (
            "backup_log INSERT失敗がexit 0のまま呼び出し元から検知不能に"
            f"なっています（doc/known-limitations.md #FF参照）。exit code={exc_info.value.code}"
        )

    def test_main_does_not_exit_on_insert_success(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", _MAIN_ARGV)
        with patch("urllib.request.urlopen"):
            record_backup_result.main()  # SystemExitを送出しなければ成功

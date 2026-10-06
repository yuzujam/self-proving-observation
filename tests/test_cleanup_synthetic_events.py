# self-proving-observation/
# └── tests/
#     └── test_cleanup_synthetic_events.py
#
# doc/known-limitations.md #SS: cleanup_synthetic_events.py のバックアップ用
# エクスポートは `curl -s`（認証ヘッダーなし・`-f`なし）をrcloneへパイプしていた。
# 2026-08-19のClickHouse認証導入後は401のエラー本文が「Parquet」として
# アップロードされ、curl・rcloneとも終了コード0のまま `--confirm` のDELETEに
# 進みえた。export_parquet() が
#   - 認証ヘッダーを付与する
#   - 非2xx・エラー本文・途中で切れたストリームを失敗として扱う
# ことを、ローカルHTTPサーバーと偽sink（標準入力をファイルへ書くだけのPython）で
# 検証する。実ClickHouse・実rcloneには依存しない。

import http.server
import importlib.util
import pathlib
import sys
import threading

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "cleanup_synthetic_events.py"

_spec = importlib.util.spec_from_file_location("cleanup_synthetic_events", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)

VALID_PARQUET = b"PAR1" + b"x" * 5000 + b"PAR1"


class _Handler(http.server.BaseHTTPRequestHandler):
    body = VALID_PARQUET
    require_user = "default"
    seen_users: list[str | None] = []

    def do_POST(self):
        user = self.headers.get("X-ClickHouse-User")
        type(self).seen_users.append(user)
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.require_user and user != self.require_user:
            payload = b"Code: 516. DB::Exception: default: Authentication failed"
            self.send_response(401)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format, *args):  # noqa: A002 - 基底クラスのシグネチャに合わせる
        pass


@pytest.fixture
def server():
    _Handler.body = VALID_PARQUET
    _Handler.require_user = "default"
    _Handler.seen_users = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()
    httpd.server_close()


def _sink_cmd(out_path: pathlib.Path) -> list[str]:
    code = (
        "import sys; "
        f"open({str(out_path)!r}, 'wb').write(sys.stdin.buffer.read())"
    )
    return [sys.executable, "-c", code]


def test_authenticated_export_succeeds_and_streams_bytes(server, tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "pw")
    out = tmp_path / "out.parquet"

    assert cleanup.export_parquet(server, "SELECT 1 FORMAT Parquet", _sink_cmd(out)) is True

    assert out.read_bytes() == VALID_PARQUET
    assert _Handler.seen_users == ["default"]


def test_missing_credentials_is_a_failure_not_an_uploaded_error_body(
    server, tmp_path, monkeypatch,
):
    # 旧実装（curl -s、認証なし）ではここで401本文がsinkへ流れ、終了コード0だった。
    monkeypatch.delenv("CLICKHOUSE_USER", raising=False)
    monkeypatch.delenv("CLICKHOUSE_PASSWORD", raising=False)
    out = tmp_path / "out.parquet"

    assert cleanup.export_parquet(server, "SELECT 1 FORMAT Parquet", _sink_cmd(out)) is False

    assert not out.exists() or b"Authentication failed" not in out.read_bytes()


def test_http_200_with_non_parquet_body_is_a_failure(server, tmp_path, monkeypatch):
    # HTTP 200でもエラー文言のみ返るケース（先頭がPAR1でない）。
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    _Handler.body = b"Code: 60. DB::Exception: Table default.threat_events doesn't exist"
    out = tmp_path / "out.parquet"

    assert cleanup.export_parquet(server, "SELECT 1 FORMAT Parquet", _sink_cmd(out)) is False


def test_truncated_stream_without_footer_is_a_failure(server, tmp_path, monkeypatch):
    # 先頭はPAR1だが末尾のフッター（PAR1）が無い＝途中で切れたエクスポート。
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    _Handler.body = b"PAR1" + b"x" * 5000
    out = tmp_path / "out.parquet"

    assert cleanup.export_parquet(server, "SELECT 1 FORMAT Parquet", _sink_cmd(out)) is False


def test_failing_sink_is_a_failure(server, monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    failing_sink = [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); sys.exit(3)"]

    assert cleanup.export_parquet(server, "SELECT 1 FORMAT Parquet", failing_sink) is False

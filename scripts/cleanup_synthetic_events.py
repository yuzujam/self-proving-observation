# self-proving-observation/
# └── scripts/
#     └── cleanup_synthetic_events.py  — threat_events から対照実験の合成注入行を削除する
#
# 対照実験（src/generator/spike.py によるinject_id付き合成トラフィック）は
# loss_rate.py がClickHouse/Elasticsearchを突合して結果をresults/*/loss_rate.jsonに
# 書き出した時点で役目を終える。生の行をClickHouseに残し続ける必要はなく、
# 2026-07-13には942,249,269行・約31.8GBがバックログとして残存し、
# ディスクを圧迫していたことが判明した（doc/known-limitations.md #K関連）。
#
# 内部指針 5.1（ClickHouseへのDELETE FROMは手動確認後にのみ実行）に従い、
# cronからは呼び出さない。MAX_RUNSを増やして対照実験を追加した場合など、
# 必要になったタイミングで手動実行する運用ツール。
#
# 使い方:
#   1. まず --dry-run で対象件数・サイズを確認
#   2. 必要なら --export-only でS3-compatible object storageにParquetバックアップのみ実行
#   3. 内容を確認した上で --confirm を付けて実際にDELETE
#
#   python3 scripts/cleanup_synthetic_events.py --dry-run
#   python3 scripts/cleanup_synthetic_events.py --export-only
#   python3 scripts/cleanup_synthetic_events.py --confirm

import argparse
import subprocess  # nosec B404 - リスト形式のみで呼び出し、shell=Trueは使用しない
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.clickhouse import (  # noqa: E402
    CLICKHOUSE_ERRORS,
    clickhouse_open,
    clickhouse_request,
)

_ROWCOUNT_QUERY = "SELECT count() FROM threat_events WHERE inject_id != ''"
_DELETE_QUERY = "ALTER TABLE threat_events DELETE WHERE inject_id != ''"

_PARQUET_MAGIC = b"PAR1"
_STREAM_CHUNK_BYTES = 1024 * 1024


def export_parquet(url: str, query: str, sink_cmd: list[str], *, timeout: int = 21600) -> bool:
    """ClickHouseのParquet出力を`sink_cmd`（rclone rcat等）の標準入力へストリーム転送する。

    以前は`curl -s`（認証ヘッダーなし・`-f`なし）をパイプしていたため、
    2026-08-19のClickHouse認証導入後は401のエラー本文が「Parquet」として
    アップロードされ、curl・rcloneとも終了コード0のまま後続のDELETEに進みえた
    （doc/known-limitations.md #SS）。ここでは
      1. `clickhouse_open`（認証ヘッダー付与・非2xxは例外）で取得する
      2. 先頭・末尾が`PAR1`（Parquetのマジック）であることを確認する
         （エラー本文や途中で切れたストリームを弾く）
      3. sinkの終了コードを確認する
    の全てを満たした場合のみTrueを返す。Falseの場合、sink側に不完全なオブジェクトが
    残りうるため、呼び出し元は削除に進まず手動確認を促すこと。
    """
    sink = subprocess.Popen(sink_cmd, stdin=subprocess.PIPE)  # nosec B603
    sink_stdin = sink.stdin
    if sink_stdin is None:  # stdin=PIPEなので通常起こらない（型の絞り込み用）
        sink.kill()
        return False
    head = b""
    tail = b""
    total = 0
    stream_ok = True
    try:
        with clickhouse_open(url, query.encode("utf-8"), timeout=timeout) as resp:
            while True:
                chunk = resp.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                if len(head) < len(_PARQUET_MAGIC):
                    head = (head + chunk)[: len(_PARQUET_MAGIC)]
                tail = (tail + chunk)[-len(_PARQUET_MAGIC):]
                total += len(chunk)
                sink_stdin.write(chunk)
    except CLICKHOUSE_ERRORS as ex:  # BrokenPipeError（sink側の異常終了）もOSErrorとして含む
        print(f"[ERROR] エクスポートのストリーム取得に失敗: {ex}", file=sys.stderr)
        stream_ok = False
    finally:
        try:
            sink_stdin.close()
        except OSError:
            stream_ok = False
        sink_code = sink.wait()

    if not stream_ok or sink_code != 0:
        return False
    if head != _PARQUET_MAGIC or tail != _PARQUET_MAGIC or total <= 2 * len(_PARQUET_MAGIC):
        print(
            f"[ERROR] 出力がParquetとして完結していません（先頭={head!r} 末尾={tail!r} "
            f"合計={total}バイト）。エラー本文または途中で切れたストリームの可能性があります。",
            file=sys.stderr,
        )
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="threat_eventsから対照実験の合成注入行（inject_id != ''）を削除する"
    )
    parser.add_argument("--clickhouse-url", default="http://localhost:8123")
    parser.add_argument("--dry-run", action="store_true", help="件数のみ表示して終了")
    parser.add_argument(
        "--export-only", action="store_true",
        help="S3-compatible object storageへParquetエクスポートのみ実行（削除しない）"
    )
    parser.add_argument(
        "--remote", default="<rclone-remote>:<bucket>/clickhouse/threat_events",
        help="rclone remote:path（--export-only時のアップロード先）"
    )
    parser.add_argument("--confirm", action="store_true", help="実際にDELETEを実行する")
    parser.add_argument(
        "--chunk-size", default="256M",
        help="rclone --s3-chunk-sizeに渡す値（既定256M）。"
        "対象行数が多く総パート数が増えるほど、S3-compatible object storage側のマルチパート"
        "アップロードで低レベルリトライ時に同一パート番号を拒否される"
        "エラー（InvalidPart/NoSuchUpload）に遭遇しやすくなるため、"
        "デフォルトのrclone既定値（5M）より大きくしてパート数自体を減らす"
        "（2026-07-13、942,849,269行のエクスポートで実際に5Mでは"
        "2回連続失敗したため確認済み。doc/known-limitations.md参照）"
    )
    args = parser.parse_args()

    url = f"{args.clickhouse_url}/"

    try:
        result = clickhouse_request(url, _ROWCOUNT_QUERY.encode(), timeout=30)
    except CLICKHOUSE_ERRORS as ex:
        print(f"[ERROR] 件数取得に失敗: {ex}", file=sys.stderr)
        sys.exit(1)

    row_count = result.decode().strip()
    print(f"[INFO] 対象行数（inject_id != ''）: {row_count}")

    if args.dry_run:
        return

    if args.export_only or args.confirm:
        import datetime

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = f"{args.remote}/synthetic_backup_{stamp}.parquet"
        print(f"[EXPORT] {dest} へエクスポート中...")
        export_query = "SELECT * FROM threat_events WHERE inject_id != '' FORMAT Parquet"
        rclone_cmd = [
            "rclone", "rcat", dest,
            "--s3-chunk-size", args.chunk_size,
            "--retries", "5",
            "--low-level-retries", "10",
        ]
        if not export_parquet(url, export_query, rclone_cmd):
            print(
                "[ERROR] エクスポートに失敗しました。削除は行いません。"
                f"不完全なオブジェクトが {dest} に残っている可能性があるため、"
                "内容を確認してから再実行してください。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("[EXPORT] 完了")

    if args.export_only:
        return

    if not args.confirm:
        print("[INFO] --confirm が指定されていないため削除しません。")
        return

    print("[DELETE] threat_events から合成注入行を削除中...")
    try:
        clickhouse_request(url, _DELETE_QUERY.encode(), timeout=120)
    except CLICKHOUSE_ERRORS as ex:
        print(f"[ERROR] 削除に失敗: {ex}", file=sys.stderr)
        sys.exit(1)
    print("[DELETE] 完了（非同期ミューテーションのためディスク使用量への反映は遅れる）")


if __name__ == "__main__":
    main()

# self-proving-observation/
# └── src/
#     └── common/
#         └── clickhouse.py  — ClickHouse HTTP API 呼び出しの共通ラッパー
#
# main.py / ml/preprocess.py / measure/loss_rate.py / receiver/worker.py で
# 個別実装されていた urllib.request の定型処理（Request 構築・urlopen・タイムアウト）
# を集約する。エラー発生時にどう振る舞うか（例外を投げる／既定値を返す等）は
# 各呼び出し元の責務のまま変えない — このモジュールは通信部分のみを担う。

import json
import os
from http.client import HTTPException, HTTPResponse
from typing import Any, cast
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# 呼び出し元で個別に except (URLError, HTTPException, OSError) するための re-export。
CLICKHOUSE_ERRORS = (URLError, HTTPException, OSError)


def _auth_headers() -> dict[str, str]:
    """CLICKHOUSE_USER/CLICKHOUSE_PASSWORD環境変数から認証ヘッダーを組み立てる。

    未設定時は空dict（認証ヘッダーなし）を返し、無認証ClickHouseに対する
    既存の呼び出し元の挙動を変えない（非破壊的拡張）。
    """
    user = os.environ.get("CLICKHOUSE_USER")
    if not user:
        return {}
    return {
        "X-ClickHouse-User": user,
        "X-ClickHouse-Key": os.environ.get("CLICKHOUSE_PASSWORD", ""),
    }


def clickhouse_request(
    url: str,
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> bytes:
    """ClickHouse HTTP API に POST し、レスポンスボディをそのまま返す。

    通信エラー（CLICKHOUSE_ERRORS）は呼び出し元で捕捉すること。
    """
    merged_headers = {**_auth_headers(), **(headers or {})}
    req = Request(url, data=body, headers=merged_headers, method="POST")  # nosec B310
    with urlopen(req, timeout=timeout) as resp:  # nosec B310
        return cast(bytes, resp.read())


def clickhouse_open(
    url: str,
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> HTTPResponse:
    """ClickHouse HTTP API に POST し、レスポンスをストリームのまま返す。

    数十GBのParquetエクスポート等、`clickhouse_request`のように全体をメモリへ
    読み込めない用途向け。認証ヘッダーは`clickhouse_request`と同じく
    `_auth_headers()`が付与する。呼び出し元は`with`文で閉じ、通信エラー
    （CLICKHOUSE_ERRORS）を捕捉すること。
    """
    merged_headers = {**_auth_headers(), **(headers or {})}
    req = Request(url, data=body, headers=merged_headers, method="POST")  # nosec B310
    return cast(HTTPResponse, urlopen(req, timeout=timeout))  # nosec B310


def query_json(
    url: str,
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = 10,
) -> list[dict[str, Any]]:
    """パラメタライズド SELECT クエリを実行し `FORMAT JSON` の結果を list[dict] で返す。

    通信エラーは呼び出し元で捕捉すること（CLICKHOUSE_ERRORS を参照）。
    """
    query_params = {f"param_{k}": v for k, v in (params or {}).items()}
    full_url = f"{url}?{urlencode(query_params)}" if query_params else url
    body = f"{sql} FORMAT JSON".encode()
    raw = clickhouse_request(full_url, body, timeout=timeout)
    result = json.loads(raw.decode("utf-8"))
    return cast(list[dict[str, Any]], result.get("data", []))

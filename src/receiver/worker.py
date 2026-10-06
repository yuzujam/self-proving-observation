# self-proving-observation/
# └── src/
#     └── receiver/
#         └── worker.py  — Redis → ClickHouse バルクインサートワーカー

import json
import os
import signal
import time
from datetime import UTC, datetime
from types import FrameType
from typing import Any, NotRequired, TypedDict, cast
from urllib.parse import urlencode

import redis

from src.common.clickhouse import CLICKHOUSE_ERRORS, clickhouse_request
from src.logging_config import get_logger

logger = get_logger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
REDIS_QUEUE_KEY = "obs:event_queue"
REDIS_HEARTBEAT_QUEUE_KEY = "obs:heartbeat_queue"
# INSERT行を組み立てられない不正イベントの隔離先（元のペイロードのまま保持、doc/pipeline-spec.md）
REDIS_DEAD_LETTER_KEY = "obs:event_dead_letter"

BATCH_SIZE = int(os.environ.get("WORKER_BATCH_SIZE", "500"))
FLUSH_INTERVAL = float(os.environ.get("WORKER_FLUSH_INTERVAL", "1.0"))

_shutdown = False

# Redis キューから取り出した生ペイロード（エッジ由来の任意JSON、構造は保証されない）
RawPayload = dict[str, Any]


class EventRow(TypedDict):
    """threat_events / threat_events_experiment への INSERT 行（proposed/init.sql と対応）。"""

    timestamp: str
    sensor_id: str
    event_type: str
    severity: int
    count: int
    inject_id: str


class HeartbeatRow(TypedDict):
    """heartbeats への INSERT 行。timestamp 省略時はテーブル側の DEFAULT now() に委ねる。"""

    node_id: str
    status: str
    timestamp: NotRequired[str]


def _handle_signal(_sig: int, _frame: FrameType | None) -> None:
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _insert_rows(table: str, rows: list[str]) -> bool:
    """指定テーブルに JSONEachRow 形式でバルクインサートする。rows が空なら何もせず成功扱い。"""
    if not rows:
        return True
    payload = "\n".join(rows).encode("utf-8")
    url = f"{CLICKHOUSE_URL}?{urlencode({'query': f'INSERT INTO {table} FORMAT JSONEachRow'})}"
    try:
        clickhouse_request(url, payload, timeout=10)
        return True
    except CLICKHOUSE_ERRORS as e:
        logger.error(f"[ERROR] ClickHouse bulk insert to {table} failed: {e}")
        return False


def insert_to_clickhouse(events: list[RawPayload]) -> int:
    """ClickHouse HTTP API に JSONEachRow 形式でバルクインサートする。

    inject_id が設定されている行（対照実験の合成注入トラフィック）は
    threat_events_experiment（短いTTLで自動失効）へ、それ以外は本体の
    threat_events へ振り分ける。90日観測データに実験ノイズを混入させない
    ための分離（doc/known-limitations.md #L）。
    """
    inserted, _failed, _invalid = _insert_to_clickhouse_detailed(events)
    return inserted


def _fmt_ts(raw: str) -> str:
    """イベント時刻をClickHouse(DateTime64(3)、UTC)向けの文字列へ整形する。

    UTCオフセット付き（例: `+0200`）の値は、オフセットを捨てず**UTCへ変換**してから
    タイムゾーン情報を外す。以前はオフセットを変換せずに捨てていたため、UTC以外の
    センサーの時刻が（例: +0200なら2時間）ずれて記録された（doc/known-limitations.md #VV）。
    オフセットなしの値はUTCとみなす（従来どおり）。
    """
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone(UTC)
        return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
    except (ValueError, AttributeError):
        # 内部指針 3.3/5.4によりタイムスタンプの現在時刻代替は禁止だが、
        # 不正な値を挿入も破棄もできないため代替はやむを得ない。
        # 「代替が起きた」事実だけは必ずログに残す（無音代替の禁止）。
        logger.error(f"[ERROR] Invalid event timestamp {raw!r}, substituting now(UTC)")
        return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:23]


def _build_event_row(ev: RawPayload) -> EventRow:
    """1イベントからINSERT用の行を組み立てる。値が不正なら ValueError/TypeError/AttributeError。"""
    severity = ev.get("severity")
    if severity is None:
        severity = (ev.get("alert") or {}).get("severity", 3)
    return {
        "timestamp": _fmt_ts(str(ev.get("timestamp", ""))),
        "sensor_id": str(ev.get("sensor_id", "unknown")),
        "event_type": str(ev.get("event_type", "unknown")),
        "severity": int(severity),
        "count": int(ev.get("count", 1)),
        "inject_id": str(ev.get("inject_id", "")),
    }


def _insert_to_clickhouse_detailed(
    events: list[RawPayload],
) -> tuple[int, list[RawPayload], list[RawPayload]]:
    """insert_to_clickhouse の内部実装。

    (挿入成功件数, 挿入に失敗したevent一覧, 行を組み立てられなかった不正event一覧) を返す。

    threat_events / threat_events_experiment は別々にINSERTするため、
    一方のみ失敗した場合に呼び出し元が全件失敗とみなして再キューすると、
    既に挿入済みの成功側まで再送され二重挿入になる。失敗した側のevent
    のみを呼び出し元へ返すことで、この二重挿入を防ぐ。

    不正event（severity・countが整数化できない等）は、例外にせず第3要素として
    返す。以前は例外がワーカーを落とし、RPOP済みのバッチ全体が消えていた
    （doc/known-limitations.md #VV）。呼び出し元が隔離リストへ移す。
    """
    if not events:
        return 0, [], []

    organic_events: list[RawPayload] = []
    organic_rows: list[str] = []
    experiment_events: list[RawPayload] = []
    experiment_rows: list[str] = []
    invalid_events: list[RawPayload] = []
    for ev in events:
        try:
            event_row = _build_event_row(ev)
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"[ERROR] Invalid event, quarantining: {e!r} payload={ev!r:.200}")
            invalid_events.append(ev)
            continue
        row = json.dumps(event_row)
        if event_row["inject_id"]:
            experiment_events.append(ev)
            experiment_rows.append(row)
        else:
            organic_events.append(ev)
            organic_rows.append(row)

    organic_ok = _insert_rows("threat_events", organic_rows)
    experiment_ok = _insert_rows("threat_events_experiment", experiment_rows)

    inserted = (
        (len(organic_rows) if organic_ok else 0)
        + (len(experiment_rows) if experiment_ok else 0)
    )
    failed_events = (
        (organic_events if not organic_ok else [])
        + (experiment_events if not experiment_ok else [])
    )
    return inserted, failed_events, invalid_events


def _quarantine_events(r: redis.Redis, events: list[RawPayload]) -> None:
    """不正イベントを元のペイロードのまま隔離リストへ移す（破棄しない）。"""
    if not events:
        return
    pipe = r.pipeline()
    for ev in events:
        pipe.lpush(REDIS_DEAD_LETTER_KEY, json.dumps(ev))
    pipe.execute()
    logger.error(
        f"[ERROR] Quarantined {len(events)} invalid events to {REDIS_DEAD_LETTER_KEY}"
    )


def _heartbeat_row(hb: RawPayload) -> HeartbeatRow:
    """heartbeatペイロードからINSERT用の行を組み立てる。

    `_received_at`（receiver/app.pyがFastAPI受信時刻を付与、doc/known-limitations.md CC）
    があれば明示的な`timestamp`を設定する。ワーカー障害で滞留したheartbeatが復旧時に
    一括INSERTされても、テーブル側の`DEFAULT now()`（=挿入時刻）ではなく実際の受信時刻を
    保持するため（内部指針 3.3「イベント発生時刻を必ず保持する。挿入時刻で代替しない」）。
    未設定・不正な値の場合は`timestamp`キー自体を省略し、既存動作どおり`DEFAULT now()`に委ねる。
    """
    row: HeartbeatRow = {
        "node_id": str(hb.get("node_id", "unknown")),
        "status": str(hb.get("status", "ok")),
    }
    received_at = hb.get("_received_at")
    if received_at is not None:
        try:
            row["timestamp"] = datetime.fromtimestamp(float(received_at), UTC).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return row


def insert_heartbeats_to_clickhouse(heartbeats: list[RawPayload]) -> int:
    """ClickHouse HTTP API に生存証明を JSONEachRow 形式でバルクインサートする。"""
    if not heartbeats:
        return 0

    rows = [json.dumps(_heartbeat_row(hb)) for hb in heartbeats]

    payload = "\n".join(rows).encode("utf-8")
    url = f"{CLICKHOUSE_URL}?{urlencode({'query': 'INSERT INTO heartbeats FORMAT JSONEachRow'})}"
    try:
        clickhouse_request(url, payload, timeout=10)
    except CLICKHOUSE_ERRORS as e:
        logger.error(f"[ERROR] ClickHouse heartbeat insert failed: {e}")
        return 0

    return len(rows)


def drain_heartbeat_queue(r: redis.Redis) -> list[RawPayload]:
    """Redis キューから生存証明を取得する。"""
    heartbeats: list[RawPayload] = []
    for _ in range(BATCH_SIZE):
        item = r.rpop(REDIS_HEARTBEAT_QUEUE_KEY)
        if item is None:
            break
        try:
            heartbeats.append(json.loads(cast(str, item)))
        except (json.JSONDecodeError, TypeError):
            logger.error(f"[ERROR] Heartbeat JSON parse failed, dropped: {item!r:.200}")
            continue
    return heartbeats


def drain_queue(r: redis.Redis) -> list[RawPayload]:
    """Redis キューからバッチサイズ分のイベントを取得する。"""
    events: list[RawPayload] = []
    for _ in range(BATCH_SIZE):
        # decode_responses=True・count省略の rpop は str | None を返すが、redis-py の型は
        # 同期/非同期・count有無を区別しない広い Union のため json.loads 直前で cast する
        item = r.rpop(REDIS_QUEUE_KEY)
        if item is None:
            break
        try:
            events.append(json.loads(cast(str, item)))
        except (json.JSONDecodeError, TypeError):
            logger.error(f"[ERROR] JSON parse failed, event dropped: {item!r:.200}")
            continue
    return events


def run() -> None:
    """メインループ — Redis からイベントを取得し ClickHouse に書き込む。"""
    r = redis.from_url(REDIS_URL, decode_responses=True)

    logger.info(f"[INFO] Worker started. batch_size={BATCH_SIZE} flush_interval={FLUSH_INTERVAL}s")
    inserted_total = 0

    while not _shutdown:
        events = drain_queue(r)

        if events:
            count, failed_events, invalid_events = _insert_to_clickhouse_detailed(events)
            _quarantine_events(r, invalid_events)
            if count > 0:
                inserted_total += count
                logger.info(f"[INFO] Inserted {count} events (total: {inserted_total})")
            if failed_events:
                # 挿入失敗分のみキューに戻してデータ欠損を防ぐ（threat_events /
                # threat_events_experiment の片方だけ失敗した場合に、既に成功
                # した側まで再送し二重挿入するのを防ぐため、失敗分に限定する）。
                # rpopで取り出した順（古い順）にrpushすると順序が反転するため、
                # reversed()で戻すことでFIFO順を保つ。
                pipe = r.pipeline()
                for ev in reversed(failed_events):
                    pipe.rpush(REDIS_QUEUE_KEY, json.dumps(ev))
                pipe.execute()
                logger.warning(f"[WARN] Insert failed, re-queued {len(failed_events)} events")
                time.sleep(FLUSH_INTERVAL)
        else:
            time.sleep(FLUSH_INTERVAL)

        heartbeats = drain_heartbeat_queue(r)
        if heartbeats:
            hb_count = insert_heartbeats_to_clickhouse(heartbeats)
            if hb_count > 0:
                logger.info(f"[INFO] Inserted {hb_count} heartbeats")
            else:
                pipe = r.pipeline()
                for hb in reversed(heartbeats):
                    pipe.rpush(REDIS_HEARTBEAT_QUEUE_KEY, json.dumps(hb))
                pipe.execute()
                logger.warning(f"[WARN] Heartbeat insert failed, re-queued {len(heartbeats)}")

    _MAX_SHUTDOWN_RETRIES = 10
    shutdown_retries = 0
    while True:
        remaining = drain_queue(r)
        if not remaining:
            break
        count, failed_events, invalid_events = _insert_to_clickhouse_detailed(remaining)
        _quarantine_events(r, invalid_events)
        if count > 0:
            inserted_total += count
        if not failed_events:
            shutdown_retries = 0
        else:
            # シャットダウン時も、挿入失敗分のみキューに戻してデータを守る
            # （既に成功した側の二重挿入を防ぐため、失敗分に限定する）
            pipe = r.pipeline()
            for ev in reversed(failed_events):
                pipe.rpush(REDIS_QUEUE_KEY, json.dumps(ev))
            pipe.execute()
            n = len(failed_events)
            shutdown_retries += 1
            logger.warning(
                f"[WARN] Shutdown insert failed ({shutdown_retries}/{_MAX_SHUTDOWN_RETRIES}), "
                f"re-queued {n} events"
            )
            if shutdown_retries >= _MAX_SHUTDOWN_RETRIES:
                logger.error(
                    f"[ERROR] Giving up after {shutdown_retries} failures. "
                    f"Data preserved in Redis (appendonly=yes)."
                )
                break
            time.sleep(FLUSH_INTERVAL)

    remaining_heartbeats = drain_heartbeat_queue(r)
    if remaining_heartbeats and insert_heartbeats_to_clickhouse(remaining_heartbeats) == 0:
        # 挿入に失敗したまま戻り値を無視すると、RPOP済みのheartbeatが黙って消え、
        # 実際には生存していた期間が偽の欠損として現れる（内部指針 3.3「静かに捨てない」、
        # doc/known-limitations.md #VV）。メインループと同様に再キューして次回起動へ残す。
        pipe = r.pipeline()
        for hb in reversed(remaining_heartbeats):
            pipe.rpush(REDIS_HEARTBEAT_QUEUE_KEY, json.dumps(hb))
        pipe.execute()
        logger.error(
            f"[ERROR] Shutdown heartbeat insert failed, re-queued {len(remaining_heartbeats)}"
        )

    logger.info(f"[INFO] Worker shutdown. Total inserted: {inserted_total}")


if __name__ == "__main__":
    run()

# self-proving-observation/
# └── tests/
#     └── test_worker.py

import json
from unittest.mock import MagicMock, patch

import pytest

from src.receiver.worker import (
    drain_heartbeat_queue,
    drain_queue,
    insert_heartbeats_to_clickhouse,
    insert_to_clickhouse,
)


class TestDrainQueue:
    def test_returns_empty_list_when_queue_empty(self):
        r = MagicMock()
        r.rpop.return_value = None
        result = drain_queue(r)
        assert result == []

    def test_returns_batch_size_items(self, monkeypatch):
        monkeypatch.setattr("src.receiver.worker.BATCH_SIZE", 3)
        r = MagicMock()
        events = [json.dumps({"id": i}) for i in range(5)]
        r.rpop.side_effect = events[:3] + [None]
        result = drain_queue(r)
        assert len(result) == 3

    def test_skips_invalid_json(self):
        r = MagicMock()
        r.rpop.side_effect = [b"not-json", json.dumps({"id": 1}), None]
        result = drain_queue(r)
        assert len(result) == 1
        assert result[0] == {"id": 1}

    def test_parsed_events_are_dicts(self):
        r = MagicMock()
        event = {"timestamp": "2026-01-01T00:00:00", "inject_id": "abc"}
        r.rpop.side_effect = [json.dumps(event), None]
        result = drain_queue(r)
        assert result == [event]


class TestInsertToClickhouse:
    def test_empty_events_returns_zero(self):
        count = insert_to_clickhouse([])
        assert count == 0

    def test_returns_event_count_on_success(self):
        events = [
            {"timestamp": "2026-01-01T00:00:00", "sensor_id": "s1",
             "event_type": "alert", "severity": 3, "count": 1, "inject_id": "id-1"},
        ]
        with patch("src.common.clickhouse.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = b""
            mock_urlopen.return_value = mock_resp
            count = insert_to_clickhouse(events)
        assert count == 1

    def test_returns_zero_on_network_error(self):
        from urllib.error import URLError
        events = [
            {"timestamp": "2026-01-01T00:00:00", "sensor_id": "s1",
             "event_type": "alert", "severity": 3, "count": 1, "inject_id": "id-1"},
        ]
        with patch("src.common.clickhouse.urlopen", side_effect=URLError("connection refused")):
            count = insert_to_clickhouse(events)
        assert count == 0

    def test_missing_fields_use_defaults(self):
        events = [{}]
        with patch("src.common.clickhouse.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = b""
            mock_urlopen.return_value = mock_resp
            count = insert_to_clickhouse(events)
        assert count == 1


class TestDrainHeartbeatQueue:
    def test_returns_empty_list_when_queue_empty(self):
        r = MagicMock()
        r.rpop.return_value = None
        result = drain_heartbeat_queue(r)
        assert result == []

    def test_skips_invalid_json(self):
        r = MagicMock()
        r.rpop.side_effect = [b"not-json", json.dumps({"node_id": "proposed"}), None]
        result = drain_heartbeat_queue(r)
        assert len(result) == 1
        assert result[0] == {"node_id": "proposed"}


class TestInsertHeartbeatsToClickhouse:
    def test_empty_heartbeats_returns_zero(self):
        count = insert_heartbeats_to_clickhouse([])
        assert count == 0

    def test_returns_heartbeat_count_on_success(self):
        heartbeats = [{"node_id": "proposed", "status": "ok"}]
        with patch("src.common.clickhouse.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = b""
            mock_urlopen.return_value = mock_resp
            count = insert_heartbeats_to_clickhouse(heartbeats)
        assert count == 1

    def test_returns_zero_on_network_error(self):
        from urllib.error import URLError
        heartbeats = [{"node_id": "proposed", "status": "ok"}]
        with patch("src.common.clickhouse.urlopen", side_effect=URLError("connection refused")):
            count = insert_heartbeats_to_clickhouse(heartbeats)
        assert count == 0

    def test_received_at_becomes_explicit_timestamp(self):
        # ワーカー障害で滞留したheartbeatが復旧時に
        # 一括INSERTされても、`_received_at`（FastAPI受信時刻）があれば挿入時刻
        # ではなく実際の受信時刻がtimestamp列に入ることを確認する。
        heartbeats = [{"node_id": "proposed", "status": "ok", "_received_at": 1755000000.0}]
        captured = {}

        def _fake_request(url, payload, timeout=10):
            captured["payload"] = payload

        with patch("src.receiver.worker.clickhouse_request", side_effect=_fake_request):
            count = insert_heartbeats_to_clickhouse(heartbeats)
        assert count == 1
        row = json.loads(captured["payload"].decode("utf-8"))
        assert row["timestamp"] == "2025-08-12 12:00:00"

    def test_missing_received_at_omits_timestamp_key(self):
        # `_received_at`がない場合はtimestampキー自体を省略し、テーブルの
        # DEFAULT now()に委ねる既存動作を変えない（後方互換性）。
        heartbeats = [{"node_id": "proposed", "status": "ok"}]
        captured = {}

        def _fake_request(url, payload, timeout=10):
            captured["payload"] = payload

        with patch("src.receiver.worker.clickhouse_request", side_effect=_fake_request):
            insert_heartbeats_to_clickhouse(heartbeats)
        row = json.loads(captured["payload"].decode("utf-8"))
        assert "timestamp" not in row

    def test_invalid_received_at_omits_timestamp_key(self):
        heartbeats = [{"node_id": "proposed", "status": "ok", "_received_at": "not-a-number"}]
        captured = {}

        def _fake_request(url, payload, timeout=10):
            captured["payload"] = payload

        with patch("src.receiver.worker.clickhouse_request", side_effect=_fake_request):
            insert_heartbeats_to_clickhouse(heartbeats)
        row = json.loads(captured["payload"].decode("utf-8"))
        assert "timestamp" not in row


def _capture_insert(events):
    """_insert_to_clickhouse_detailedを呼び、ClickHouseへ送られた行（テーブル別）も返す。"""
    from src.receiver.worker import _insert_to_clickhouse_detailed

    sent: dict[str, list[dict]] = {}

    def _fake_request(url, payload, timeout=10):
        table = url.split("INSERT+INTO+")[1].split("+FORMAT")[0]
        sent.setdefault(table, []).extend(
            json.loads(line) for line in payload.decode("utf-8").splitlines()
        )

    with patch("src.receiver.worker.clickhouse_request", side_effect=_fake_request):
        result = _insert_to_clickhouse_detailed(events)
    return result, sent


class TestEventTimestampNormalization:
    """UTCオフセット付きのイベント時刻は、オフセットを捨てず（=時刻がずれる）
    UTCへ変換してから格納する。オフセットなしはUTCとみなす（従来どおり）。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-09-24T12:00:00.123456+0200", "2026-09-24 10:00:00.123"),
            ("2026-09-24T12:00:00.123456+02:00", "2026-09-24 10:00:00.123"),
            ("2026-09-24T12:00:00.123456+0000", "2026-09-24 12:00:00.123"),
            ("2026-09-24T12:00:00.123456", "2026-09-24 12:00:00.123"),
            ("2026-09-24T00:30:00+0100", "2026-09-23 23:30:00.000"),
        ],
    )
    def test_offset_is_converted_to_utc(self, raw, expected):
        (_, _, _), sent = _capture_insert(
            [{"timestamp": raw, "sensor_id": "s", "event_type": "alert", "inject_id": ""}]
        )
        assert sent["threat_events"][0]["timestamp"] == expected


class TestInvalidEventsAreSeparated:
    """severity・countが整数化できない等、行を組み立てられないイベントで
    例外を投げるとワーカーが落ち、RPOP済みのバッチ全体が消えていた。"""

    def test_poison_event_is_returned_separately_and_good_events_still_inserted(self):
        poison = {"timestamp": "2026-01-01T00:00:00", "severity": "high", "sensor_id": "bad"}
        good = {"timestamp": "2026-01-01T00:00:00", "severity": 2, "sensor_id": "ok"}

        (inserted, failed, invalid), sent = _capture_insert([poison, good])

        assert inserted == 1
        assert failed == []
        assert invalid == [poison]
        assert [row["sensor_id"] for row in sent["threat_events"]] == ["ok"]

    @pytest.mark.parametrize(
        "bad_event",
        [
            {"count": "many"},
            {"alert": "not-an-object"},
            {"severity": [1]},
        ],
    )
    def test_various_malformed_values_are_isolated(self, bad_event):
        (inserted, _failed, invalid), sent = _capture_insert([bad_event])

        assert inserted == 0
        assert invalid == [bad_event]
        assert sent == {}

    def test_public_wrapper_keeps_returning_only_the_inserted_count(self):
        good = {"timestamp": "2026-01-01T00:00:00", "severity": 2}
        poison = {"severity": "high"}
        with patch("src.receiver.worker.clickhouse_request"):
            assert insert_to_clickhouse([good, poison]) == 1


class TestShutdownDrain:
    """シャットダウン時の最終ドレインでも、取り出したデータは挿入成功・再キュー・
    隔離のいずれかに帰着する。"""

    @pytest.fixture
    def fake_redis(self, monkeypatch):
        import fakeredis

        from src.receiver import worker

        server = fakeredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(worker.redis, "from_url", lambda *a, **k: server)
        monkeypatch.setattr(worker, "_shutdown", True)  # メインループを飛ばして最終ドレインへ
        monkeypatch.setattr(worker.time, "sleep", lambda *_a: None)
        return server

    def test_heartbeats_are_requeued_when_final_insert_fails(self, fake_redis):
        from urllib.error import URLError

        from src.receiver import worker

        hb = {"node_id": "baseline", "status": "ok", "_received_at": 1755000000.0}
        fake_redis.lpush(worker.REDIS_HEARTBEAT_QUEUE_KEY, json.dumps(hb))

        with patch("src.receiver.worker.clickhouse_request", side_effect=URLError("down")):
            worker.run()

        assert fake_redis.llen(worker.REDIS_HEARTBEAT_QUEUE_KEY) == 1
        assert json.loads(fake_redis.lindex(worker.REDIS_HEARTBEAT_QUEUE_KEY, 0)) == hb

    def test_heartbeats_are_not_requeued_when_final_insert_succeeds(self, fake_redis):
        from src.receiver import worker

        fake_redis.lpush(
            worker.REDIS_HEARTBEAT_QUEUE_KEY, json.dumps({"node_id": "proposed", "status": "ok"})
        )

        with patch("src.receiver.worker.clickhouse_request"):
            worker.run()

        assert fake_redis.llen(worker.REDIS_HEARTBEAT_QUEUE_KEY) == 0

    def test_invalid_events_go_to_dead_letter_list_and_do_not_stop_the_worker(self, fake_redis):
        from src.receiver import worker

        poison = {"severity": "high", "sensor_id": "bad"}
        good = {"timestamp": "2026-01-01T00:00:00", "severity": 2, "sensor_id": "ok"}
        fake_redis.lpush(worker.REDIS_QUEUE_KEY, json.dumps(poison))
        fake_redis.lpush(worker.REDIS_QUEUE_KEY, json.dumps(good))

        with patch("src.receiver.worker.clickhouse_request"):
            worker.run()  # 例外で落ちないこと

        assert fake_redis.llen(worker.REDIS_QUEUE_KEY) == 0
        assert [json.loads(x) for x in fake_redis.lrange(worker.REDIS_DEAD_LETTER_KEY, 0, -1)] == [
            poison
        ]

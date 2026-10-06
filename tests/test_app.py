# self-proving-observation/
# └── tests/
#     └── test_app.py

import json

import pytest
from fastapi.testclient import TestClient

import src.receiver.app as app_module
from src.receiver.app import (
    REDIS_COUNTER_KEY,
    REDIS_HEARTBEAT_QUEUE_KEY,
    REDIS_QUEUE_KEY,
    app,
)


class FakePipeline:
    """redis.asyncio の pipeline 互換スタブ。execute() まで実際の書き込みを遅延する。"""

    def __init__(self, redis: "FakeRedis"):
        self._redis = redis
        self._pushes: list[tuple[str, str]] = []
        self._increments: list[tuple[str, int]] = []

    def lpush(self, key: str, value: str) -> None:
        self._pushes.append((key, value))

    def incrby(self, key: str, amount: int) -> None:
        self._increments.append((key, amount))

    async def execute(self) -> None:
        if self._redis.fail:
            raise ConnectionError("redis down")
        for key, value in self._pushes:
            self._redis.lists.setdefault(key, []).insert(0, value)
        for key, amount in self._increments:
            self._redis.counters[key] = self._redis.counters.get(key, 0) + amount


class FakeRedis:
    """受付レイヤーが使う redis.asyncio.Redis のメソッドのみを持つインメモリスタブ。"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.lists: dict[str, list[str]] = {}
        self.counters: dict[str, int] = {}

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def lpush(self, key: str, value: str) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.lists.setdefault(key, []).insert(0, value)

    async def ping(self) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        return True

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    async def get(self, key: str) -> str | None:
        value = self.counters.get(key)
        return None if value is None else str(value)


@pytest.fixture
def client():
    # lifespan（実 Redis への接続）を走らせないよう、with を使わずに生成する
    return TestClient(app)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(app_module, "_redis_pool", fake)
    return fake


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(app_module, "_redis_pool", None)


class TestReceiveEvents:
    def test_single_event_is_queued_with_received_at(self, client, fake_redis):
        event = {"timestamp": "2026-01-01T00:00:00", "sensor_id": "edge-1", "count": 3}
        resp = client.post("/events", content=json.dumps(event))

        assert resp.status_code == 202
        assert resp.json() == {"accepted": 1}
        queued = [json.loads(v) for v in fake_redis.lists[REDIS_QUEUE_KEY]]
        assert len(queued) == 1
        assert queued[0]["sensor_id"] == "edge-1"
        # イベント発生時刻は保持し、受信時刻は別フィールドとして付与する（内部指針 3.3）
        assert queued[0]["timestamp"] == "2026-01-01T00:00:00"
        assert isinstance(queued[0]["_received_at"], float)

    def test_event_list_is_queued_and_counted(self, client, fake_redis):
        events = [{"sensor_id": f"edge-{i}"} for i in range(3)]
        resp = client.post("/events", content=json.dumps(events))

        assert resp.status_code == 202
        assert resp.json() == {"accepted": 3}
        assert len(fake_redis.lists[REDIS_QUEUE_KEY]) == 3
        assert fake_redis.counters[REDIS_COUNTER_KEY] == 3

    def test_invalid_json_returns_400(self, client, fake_redis):
        resp = client.post("/events", content=b"not-json")

        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid JSON"}
        assert REDIS_QUEUE_KEY not in fake_redis.lists

    @pytest.mark.parametrize("body", ['"abc"', "42", "null", "[1, 2]", "true"])
    def test_non_object_events_return_400_not_503(self, client, fake_redis, body):
        # #XX: 以前は要素への`event["_received_at"] = ...`が例外となり、クライアントの
        # 誤りが「503 queue unavailable」（サーバー障害、送信側は再送し続ける）として
        # 返り、例外文言も応答に含まれていた。
        resp = client.post("/events", content=body)

        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid event: expected JSON object(s)"}
        assert REDIS_QUEUE_KEY not in fake_redis.lists
        assert REDIS_COUNTER_KEY not in fake_redis.counters

    def test_batch_with_one_non_object_is_rejected_as_a_whole(self, client, fake_redis):
        # 一部だけ投入すると、送信側の再送で有効イベントが二重に入る。全体を拒否する。
        body = json.dumps([{"sensor_id": "edge-1"}, "oops", {"sensor_id": "edge-2"}])
        resp = client.post("/events", content=body)

        assert resp.status_code == 400
        assert REDIS_QUEUE_KEY not in fake_redis.lists

    def test_empty_batch_is_still_accepted(self, client, fake_redis):
        resp = client.post("/events", content=b"[]")

        assert resp.status_code == 202
        assert resp.json() == {"accepted": 0}

    def test_redis_failure_returns_503(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "_redis_pool", FakeRedis(fail=True))
        resp = client.post("/events", content=json.dumps({"sensor_id": "edge-1"}))

        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "queue unavailable"
        assert "redis down" in body["detail"]

    def test_returns_503_before_startup(self, client, no_redis):
        resp = client.post("/events", content=json.dumps({"sensor_id": "edge-1"}))

        assert resp.status_code == 503
        assert resp.json() == {"error": "service unavailable"}


class TestReceiveHeartbeat:
    def test_heartbeat_is_normalized_and_queued(self, client, fake_redis):
        resp = client.post(
            "/heartbeat",
            content=json.dumps({"node_id": "baseline", "status": "ok", "extra": "dropped"}),
        )

        assert resp.status_code == 202
        assert resp.json() == {"accepted": True}
        queued = [json.loads(v) for v in fake_redis.lists[REDIS_HEARTBEAT_QUEUE_KEY]]
        assert len(queued) == 1
        assert set(queued[0]) == {"node_id", "status", "_received_at"}
        assert queued[0]["node_id"] == "baseline"
        assert queued[0]["status"] == "ok"

    def test_missing_fields_fall_back_to_defaults(self, client, fake_redis):
        resp = client.post("/heartbeat", content=b"{}")

        assert resp.status_code == 202
        queued = json.loads(fake_redis.lists[REDIS_HEARTBEAT_QUEUE_KEY][0])
        assert queued["node_id"] == "unknown"
        assert queued["status"] == "ok"

    def test_invalid_json_returns_400(self, client, fake_redis):
        resp = client.post("/heartbeat", content=b"{broken")

        assert resp.status_code == 400
        assert REDIS_HEARTBEAT_QUEUE_KEY not in fake_redis.lists

    @pytest.mark.parametrize("body", ['"abc"', "42", "null", "[]", "[1]"])
    def test_non_object_heartbeat_returns_400(self, client, fake_redis, body):
        # #XX: 以前は`payload.get`のAttributeErrorが未捕捉で500になっていた。
        resp = client.post("/heartbeat", content=body)

        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid heartbeat: expected JSON object"}
        assert REDIS_HEARTBEAT_QUEUE_KEY not in fake_redis.lists

    def test_redis_failure_returns_503(self, client, monkeypatch):
        # 失敗時に 202 を返すと送信側は成功とみなし、欠損が「行の不在」として残らなくなる
        monkeypatch.setattr(app_module, "_redis_pool", FakeRedis(fail=True))
        resp = client.post("/heartbeat", content=json.dumps({"node_id": "proposed"}))

        assert resp.status_code == 503
        assert resp.json()["error"] == "queue unavailable"

    def test_returns_503_before_startup(self, client, no_redis):
        resp = client.post("/heartbeat", content=json.dumps({"node_id": "proposed"}))

        assert resp.status_code == 503


class TestHealth:
    def test_healthy_reports_queue_length_and_total(self, client, fake_redis):
        client.post("/events", content=json.dumps([{"a": 1}, {"a": 2}]))
        resp = client.get("/health")

        assert resp.status_code == 200
        assert resp.json() == {"status": "healthy", "queue_length": 2, "total_received": 2}

    def test_total_defaults_to_zero(self, client, fake_redis):
        resp = client.get("/health")

        assert resp.status_code == 200
        assert resp.json()["total_received"] == 0

    def test_unhealthy_when_redis_down(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "_redis_pool", FakeRedis(fail=True))
        resp = client.get("/health")

        assert resp.status_code == 503
        assert resp.json()["status"] == "unhealthy"

    def test_starting_before_startup(self, client, no_redis):
        resp = client.get("/health")

        assert resp.status_code == 503
        assert resp.json() == {"status": "starting"}


class TestOpenApiSchema:
    def test_response_model_not_inferred_from_annotations(self):
        # 戻り値の型注釈追加後も、response_model=None により注釈前と同じく
        # レスポンススキーマが生成されない（=レスポンス検証・変換が挟まらない）ことを確認する
        schema = app.openapi()
        for path, method in (("/events", "post"), ("/heartbeat", "post"), ("/health", "get")):
            responses = schema["paths"][path][method]["responses"]
            success = next(v for k, v in responses.items() if k.startswith("2"))
            assert success["content"]["application/json"]["schema"] == {}

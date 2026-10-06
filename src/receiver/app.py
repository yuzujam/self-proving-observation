# self-proving-observation/
# └── src/
#     └── receiver/
#         └── app.py  — FastAPI 受付レイヤー（非同期キューイング）

import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response, status

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
REDIS_QUEUE_KEY = "obs:event_queue"
REDIS_COUNTER_KEY = "obs:received_total"
REDIS_HEARTBEAT_QUEUE_KEY = "obs:heartbeat_queue"

_redis_pool: aioredis.Redis | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _redis_pool
    _redis_pool = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield
    if _redis_pool:
        await _redis_pool.aclose()


app = FastAPI(
    title="Event Receiver",
    lifespan=lifespan,
)


@app.post("/events", status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def receive_events(request: Request) -> Response | dict[str, int]:
    """集約済みイベントを受け取り Redis キューに格納する。"""
    if _redis_pool is None:
        return Response(
            content='{"error":"service unavailable"}',
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="application/json",
        )

    body = await request.body()

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Response(
            content='{"error":"invalid JSON"}',
            status_code=status.HTTP_400_BAD_REQUEST,
            media_type="application/json",
        )

    events = payload if isinstance(payload, list) else [payload]

    # JSONオブジェクトでない要素があると、下の`event["_received_at"] = ...`が例外となり
    # クライアントの誤りが503（サーバー障害）として返っていた。Redisへ触れる前に
    # 全体を検証して400を返す（一部だけ投入すると再送で有効イベントが二重に入る）。
    if not all(isinstance(event, dict) for event in events):
        return Response(
            content='{"error":"invalid event: expected JSON object(s)"}',
            status_code=status.HTTP_400_BAD_REQUEST,
            media_type="application/json",
        )

    try:
        pipe = _redis_pool.pipeline()
        for event in events:
            event["_received_at"] = time.time()
            pipe.lpush(REDIS_QUEUE_KEY, json.dumps(event))
        pipe.incrby(REDIS_COUNTER_KEY, len(events))
        await pipe.execute()
    except Exception as e:
        return Response(
            content=json.dumps({"error": "queue unavailable", "detail": str(e)}),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="application/json",
        )

    return {"accepted": len(events)}


@app.post("/heartbeat", status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def receive_heartbeat(request: Request) -> Response | dict[str, bool]:
    """拠点からの生存証明（1分単位）を受け取り Redis キューに格納する。"""
    if _redis_pool is None:
        return Response(
            content='{"error":"service unavailable"}',
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="application/json",
        )

    body = await request.body()

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Response(
            content='{"error":"invalid JSON"}',
            status_code=status.HTTP_400_BAD_REQUEST,
            media_type="application/json",
        )

    if not isinstance(payload, dict):
        return Response(
            content='{"error":"invalid heartbeat: expected JSON object"}',
            status_code=status.HTTP_400_BAD_REQUEST,
            media_type="application/json",
        )

    node_id = str(payload.get("node_id", "unknown"))
    hb_status = str(payload.get("status", "ok"))

    try:
        await _redis_pool.lpush(
            REDIS_HEARTBEAT_QUEUE_KEY,
            json.dumps({
                "node_id": node_id,
                "status": hb_status,
                "_received_at": time.time(),
            }),
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": "queue unavailable", "detail": str(e)}),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="application/json",
        )

    return {"accepted": True}


@app.get("/health", response_model=None)
async def health() -> Response | dict[str, Any]:
    """ヘルスチェック — Redis 接続確認。"""
    if _redis_pool is None:
        return Response(
            content='{"status":"starting"}',
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="application/json",
        )
    try:
        await _redis_pool.ping()
        queue_len = await _redis_pool.llen(REDIS_QUEUE_KEY)
        total = await _redis_pool.get(REDIS_COUNTER_KEY) or "0"
        return {
            "status": "healthy",
            "queue_length": queue_len,
            "total_received": int(total),
        }
    except Exception as e:
        return Response(
            content=json.dumps({"status": "unhealthy", "error": str(e)}),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="application/json",
        )

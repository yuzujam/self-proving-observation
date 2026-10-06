# self-proving-observation/
# └── src/
#     └── ml/
#         └── preprocess.py  — ClickHouse → 時系列特徴量抽出

import os
from datetime import datetime
from typing import Any

import numpy as np

from src.common.clickhouse import CLICKHOUSE_ERRORS, query_json
from src.logging_config import get_logger

logger = get_logger(__name__)

# 注意（doc/known-limitations.md #AAA）: このモジュールのClickHouse抽出（extract_windowed_features /
# prepare_dataset）は現状どこからも呼ばれていない。確定済みのFidelity Guard実験
# （scripts/run_fidelity_experiment.py）は合成データで動作する。実データへ適用する場合は、
#   - 集計対象が生の threat_events（対照実験の混入・自ノードIP・sensor_id 空/unknown を含む）で
#     あること（分析用には threat_events_clean VIEW を検討する）
#   - 空の窓は結果に現れず、時間的に隣接しない窓がシーケンス上は隣接すること
# に注意すること。後者は下の _count_missing_windows で警告として明示する。

CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "http://localhost:8123")

WINDOW_MINUTES = 5
SEQUENCE_LENGTH = 12


def query_clickhouse(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """ClickHouse HTTP API にパラメタライズドクエリを実行する。"""
    try:
        return query_json(CLICKHOUSE_URL, sql, params, timeout=30)
    except CLICKHOUSE_ERRORS as e:
        raise RuntimeError(f"ClickHouse query failed: {e}") from e


def _count_missing_windows(rows: list[dict[str, Any]], window_minutes: int = WINDOW_MINUTES) -> int:
    """連続する window_start の間に欠けている（イベントが1件も無い）窓の総数を返す。

    GROUP BY は空の窓を出力しないため、観測の空白（イベント無し、または観測欠損）が
    黙って詰められ、時間的に離れた窓がシーケンス上で隣接する。欠損を隠さない
    （内部指針 5.4）ため件数を数えて警告する。値の補間・0埋めはしない。
    """
    starts = [datetime.fromisoformat(str(r["window_start"])) for r in rows if r.get("window_start")]
    missing = 0
    for prev, cur in zip(starts, starts[1:], strict=False):
        gap_windows = int((cur - prev).total_seconds() // (window_minutes * 60)) - 1
        if gap_windows > 0:
            missing += gap_windows
    return missing


def extract_windowed_features(hours: int = 24) -> np.ndarray:
    """時間窓ごとの特徴量ベクトルを ClickHouse から抽出する。"""
    sql = """
    SELECT
        toStartOfInterval(timestamp, INTERVAL {window_minutes:UInt32} MINUTE) AS window_start,
        count()                          AS event_count,
        uniqExact(sensor_id)             AS unique_sensors,
        avg(severity)                    AS avg_severity,
        max(severity)                    AS max_severity,
        countIf(event_type = 'alert')    AS alert_count,
        countIf(event_type = 'dns')      AS dns_count,
        countIf(event_type = 'http')     AS http_count,
        countIf(event_type = 'tls')      AS tls_count,
        countIf(event_type = 'flow')     AS flow_count,
        countIf(event_type = 'ssh')      AS ssh_count
    FROM threat_events
    WHERE timestamp >= now() - toIntervalHour({hours:UInt32})
    GROUP BY window_start
    ORDER BY window_start
    """
    rows = query_clickhouse(sql, params={"window_minutes": WINDOW_MINUTES, "hours": hours})

    if not rows:
        return np.array([])

    missing = _count_missing_windows(rows)
    if missing:
        logger.warning(
            f"[WARN] {missing} 個の{WINDOW_MINUTES}分窓にイベントが無く結果から欠けています"
            "（観測欠損か無攻撃かは区別できません。Heartbeatカバレッジで確認してください）"
        )

    feature_keys = [
        "event_count", "unique_sensors", "avg_severity", "max_severity",
        "alert_count", "dns_count", "http_count", "tls_count",
        "flow_count", "ssh_count",
    ]
    features = []
    for row in rows:
        vec = [float(row.get(k, 0)) for k in feature_keys]
        features.append(vec)

    return np.array(features, dtype=np.float32)


def create_sequences(
    features: np.ndarray,
    seq_len: int = SEQUENCE_LENGTH,
) -> tuple[np.ndarray, np.ndarray]:
    """sliding window でシーケンスデータを生成する。"""
    if len(features) <= seq_len:
        raise ValueError(
            f"Not enough data: {len(features)} windows, need > {seq_len}"
        )

    X, y = [], []
    for i in range(len(features) - seq_len):
        X.append(features[i : i + seq_len])
        y.append(features[i + seq_len])
    return np.array(X), np.array(y)


def normalize(data: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """特徴量を正規化する（平均0、標準偏差1）。"""
    mean = data.mean(axis=0)
    std = data.std(axis=0)
    std[std == 0] = 1.0
    return (data - mean) / std, mean, std


def prepare_dataset(
    hours: int = 24,
    seq_len: int = SEQUENCE_LENGTH,
    train_ratio: float = 0.8,
) -> dict[str, np.ndarray]:
    """学習・テストデータセットを準備する。"""
    features = extract_windowed_features(hours)
    if len(features) == 0:
        raise ValueError("No data available from ClickHouse")

    normalized, mean, std = normalize(features)
    X, y = create_sequences(normalized, seq_len)

    split = int(len(X) * train_ratio)
    return {
        "X_train": X[:split],
        "y_train": y[:split],
        "X_test": X[split:],
        "y_test": y[split:],
        "mean": mean,
        "std": std,
    }

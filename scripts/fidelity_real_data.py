# self-proving-observation/
# └── scripts/
#     └── fidelity_real_data.py
#
# Fidelity Guard を、合成時系列ではなく観測データ（日次Parquet）に適用する補足実験
# （論文6.4節の限界への対応。2026-10-02、doc/decisions.md）。
# 日次の清浄件数は2026-07-20に約1.5〜1.8倍へ水準が上がり戻らなかった（6.3節）。この水準変化を
# 「おおよその開始時点が既知のドリフト」として、正常期間で学習したLSTM＋SHAPの検知が
# 精度低下（MSEが正常期間の平均の3倍超）とどの順で起きるかを、モデルの乱数seed別に調べる。
# 開始時点が日単位でしか分からない単一の事象で、シナリオの再現ではなく1事例の確認にとどまる。
#
# 入力: rclone copy "<remote>:<bucket>/clickhouse/threat_events/" <DIR> で取得した日次Parquet。
# 特徴量（1時間ごと）: 清浄な件数合計・ユニーク送信元数・主なイベント種別別の件数。
# 清浄の定義は6.3節と同じ（inject_id・自ノードIP・空/unknown sensor_idを除外）。
#
# 使い方:
#   python scripts/fidelity_real_data.py DIR --normal 2026-07-14 2026-07-19 \
#       --test 2026-07-20 2026-07-31 --seeds 10 --out result.json
#   対照（水準変化なしの期間）: --normal 2026-07-24 2026-07-27 --test 2026-07-28 2026-07-31

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ml.fidelity_guard import FidelityGuard  # noqa: E402
from src.ml.lstm_model import predict, train_model  # noqa: E402
from src.ml.preprocess import SEQUENCE_LENGTH, create_sequences, normalize  # noqa: E402

NODE_IPS = ("203.0.113.10", "203.0.113.11")
EVENT_TYPES = ("flow", "snmp", "rdp", "alert", "ssh", "http")
WINDOW_SIZE = 10
MAX_CONFIRMING_LAG = 8
NSAMPLES = 300


def hourly_features(data_dir: Path, start: date, end: date) -> pd.DataFrame:
    frames = []
    day = start
    while day <= end:
        path = data_dir / f"threat_events_{day.isoformat()}.parquet"
        cols = ["timestamp", "sensor_id", "event_type", "count", "inject_id"]
        table = pq.read_table(path, columns=cols)
        df = table.to_pandas()
        keep = (
            (df["inject_id"] == "")
            & ~df["sensor_id"].isin(list(NODE_IPS) + ["", "unknown"])
        )
        frames.append(df[keep])
        day += timedelta(days=1)
    df = pd.concat(frames)
    df["hour"] = df["timestamp"].dt.floor("h")
    first = pd.Timestamp(start, tz="UTC")
    last = pd.Timestamp(end, tz="UTC") + pd.Timedelta(hours=23)
    feats = pd.DataFrame(index=pd.date_range(first, last, freq="h"))
    feats["event_count"] = df.groupby("hour")["count"].sum()
    feats["unique_sensors"] = df.groupby("hour")["sensor_id"].nunique()
    for et in EVENT_TYPES:
        feats[f"{et}_count"] = df[df["event_type"] == et].groupby("hour")["count"].sum()
    return feats.fillna(0.0)


def run_seed(feats: pd.DataFrame, n_normal: int, seed: int) -> dict[str, object]:
    np.random.seed(seed)
    torch.manual_seed(seed)
    values = feats.to_numpy(dtype=np.float32)
    normal, mean, std = normalize(values[:n_normal])
    x_train, y_train = create_sequences(normal, SEQUENCE_LENGTH)
    split = int(len(x_train) * 0.8)
    model = train_model(x_train[:split], y_train[:split], epochs=50, batch_size=32)
    x_full, y_full = create_sequences((values - mean) / std, SEQUENCE_LENGTH)
    n_windows = len(x_full) // WINDOW_SIZE
    windows = [x_full[i * WINDOW_SIZE:(i + 1) * WINDOW_SIZE] for i in range(n_windows)]
    drift_window = (n_normal - SEQUENCE_LENGTH) // WINDOW_SIZE
    fg = FidelityGuard(model, x_train[:split][:20])
    results = fg.monitor_drift(
        windows, window_threshold=0.5, nsamples=NSAMPLES,
        threshold_mode="adaptive", baseline_windows=max(drift_window, 1),
    )
    mses = []
    for i in range(n_windows):
        sl = slice(i * WINDOW_SIZE, (i + 1) * WINDOW_SIZE)
        preds = predict(model, x_full[sl])
        mses.append(float(np.mean((preds - y_full[sl]) ** 2)))
    baseline = float(np.mean(mses[:drift_window]))
    first_acc = next((i for i in range(drift_window, n_windows) if mses[i] > 3 * baseline), None)
    first_fid = next((r["window_index"] for r in results if r["drift_detected"]), None)
    lag = None if first_acc is None or first_fid is None else first_fid - first_acc
    return {
        "seed": seed,
        "n_windows": n_windows,
        "drift_window": drift_window,
        "first_accuracy_drop_window": first_acc,
        "first_shap_detection_window": first_fid,
        "detection_lag": lag,
        "confirming_detected": lag is not None and lag <= MAX_CONFIRMING_LAG,
        "false_alarm_windows_before_drift": [
            r["window_index"] for r in results
            if r["drift_detected"] and r["window_index"] < drift_window
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("dir")
    ap.add_argument("--normal", nargs=2, required=True, metavar=("START", "END"))
    ap.add_argument("--test", nargs=2, required=True, metavar=("START", "END"))
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    n0, n1 = (date.fromisoformat(v) for v in args.normal)
    t0, t1 = (date.fromisoformat(v) for v in args.test)
    feats = hourly_features(Path(args.dir), n0, t1)
    normal_end = pd.Timestamp(n1, tz="UTC") + pd.Timedelta(hours=24)
    n_normal = int((normal_end - feats.index[0]) / pd.Timedelta(hours=1))
    test_hours = len(feats) - n_normal
    print(f"hours={len(feats)} normal_hours={n_normal} test_hours={test_hours} (test {t0}..{t1})")
    runs = []
    for s in range(1, args.seeds + 1):
        r = run_seed(feats, n_normal, 100 + s)
        runs.append(r)
        print(r, flush=True)
    det = [r for r in runs if r["detection_lag"] is not None]
    print(f"seeds={len(runs)} detected={len(det)} lags={sorted(r['detection_lag'] for r in det)}")
    if args.out:
        Path(args.out).write_text(json.dumps(runs, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

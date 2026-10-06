# self-proving-observation/
# └── tests/
#     └── test_fidelity_guard.py
#
# doc/known-limitations.md #WW: FidelityGuard.monitor_drift() は、SHAPベースライン確立前の
# 窓で `moving_avg < effective_threshold`（numpy.float64同士の比較）をそのまま返していた。
# 結果は numpy.bool_ で、run_fidelity_experiment.py の json.dump(default=str) が
# 文字列 "False" として書き出す（ベースライン確立後の窓はPythonのboolなので `false`）。
# 文字列 "False" は再読込すると真と評価されるため、JSONを読み直して drift_detected の
# 真偽で判定するコードは、ベースライン確立前の窓を全て「検知」と誤読する。
# 実際のFidelity実験（n=80）は判定をメモリ上の値で行うため結果は無傷だった。
# 極小のモデル・入力でSHAPを走らせ、型が常にPythonのboolであることを検証する。

import json

import numpy as np
import pytest
import torch

from src.ml.fidelity_guard import FidelityGuard
from src.ml.lstm_model import ThreatLSTM


@pytest.fixture(scope="module")
def guard_and_windows():
    torch.manual_seed(0)
    rng = np.random.default_rng(0)
    model = ThreatLSTM(input_dim=2, hidden_dim=8, num_layers=1)
    background = rng.standard_normal((6, 3, 2)).astype(np.float32)
    windows = [rng.standard_normal((4, 3, 2)).astype(np.float32) for _ in range(8)]
    return FidelityGuard(model, background), windows


@pytest.mark.parametrize("threshold_mode", ["absolute", "adaptive"])
def test_drift_detected_is_a_plain_bool_in_every_window(guard_and_windows, threshold_mode):
    guard, windows = guard_and_windows

    results = guard.monitor_drift(
        windows, nsamples=20, threshold_mode=threshold_mode, baseline_windows=4,
    )

    assert len(results) == len(windows)
    for r in results:
        assert type(r["drift_detected"]) is bool, (r["window_index"], type(r["drift_detected"]))


def test_drift_detected_survives_a_json_round_trip_as_booleans(guard_and_windows):
    guard, windows = guard_and_windows

    results = guard.monitor_drift(
        windows, nsamples=20, threshold_mode="adaptive", baseline_windows=4,
    )
    # run_fidelity_experiment.py と同じ書き出し方（default=str）
    reloaded = json.loads(json.dumps(results, default=str))

    assert [r["drift_detected"] for r in reloaded] == [r["drift_detected"] for r in results]
    assert all(isinstance(r["drift_detected"], bool) for r in reloaded)

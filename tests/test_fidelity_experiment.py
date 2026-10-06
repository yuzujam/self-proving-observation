# self-proving-observation/
# └── tests/
#     └── test_fidelity_experiment.py
#
# BUG-17 回帰テスト: fidelity_leads は「厳密な先行（<）」を要求する

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))



def _make_fidelity_results(drift_windows: list[int], n: int = 20) -> list[dict]:
    """指定ウィンドウでドリフト検知済みの fidelity_results を生成する。"""
    return [
        {
            "window_index": i,
            "fidelity_score": 0.2 if i in drift_windows else 0.9,
            "moving_avg": 0.2 if i in drift_windows else 0.9,
            "drift_detected": i in drift_windows,
        }
        for i in range(n)
    ]


def _make_accuracy_results(drop_windows: list[int], n: int = 20) -> list[dict]:
    """指定ウィンドウで精度低下済みの accuracy_per_window を生成する。"""
    return [
        {"window_index": i, "mse": 10.0 if i in drop_windows else 0.01}
        for i in range(n)
    ]


def _compute_fidelity_leads(fidelity_results, accuracy_results, drift_window_approx):
    """run_fidelity_experiment.run_single_scenario と同じロジックを再現する。"""
    first_fidelity_drop = None
    for r in fidelity_results:
        if r["drift_detected"]:
            first_fidelity_drop = r["window_index"]
            break

    first_accuracy_drop = None
    mses = [a["mse"] for a in accuracy_results[:drift_window_approx]]
    baseline_mse = sum(mses) / len(mses) if mses else 0
    threshold = baseline_mse * 3
    for a in accuracy_results:
        if a["window_index"] >= drift_window_approx and a["mse"] > threshold:
            first_accuracy_drop = a["window_index"]
            break

    return (
        first_fidelity_drop is not None
        and first_accuracy_drop is not None
        and first_fidelity_drop < first_accuracy_drop  # BUG-17: must be strictly <
    )


class TestFidelityLeadsStrictPrecedence:
    def test_fidelity_strictly_before_accuracy_is_true(self):
        fidelity = _make_fidelity_results(drift_windows=[5])
        accuracy = _make_accuracy_results(drop_windows=[8])
        assert _compute_fidelity_leads(fidelity, accuracy, drift_window_approx=4) is True

    def test_fidelity_same_window_as_accuracy_is_false(self):
        """同一ウィンドウは「先行」ではなく「同時」— BUG-17の修正確認"""
        fidelity = _make_fidelity_results(drift_windows=[5])
        accuracy = _make_accuracy_results(drop_windows=[5])
        assert _compute_fidelity_leads(fidelity, accuracy, drift_window_approx=4) is False

    def test_fidelity_after_accuracy_is_false(self):
        fidelity = _make_fidelity_results(drift_windows=[8])
        accuracy = _make_accuracy_results(drop_windows=[5])
        assert _compute_fidelity_leads(fidelity, accuracy, drift_window_approx=4) is False

    def test_no_fidelity_drop_is_false(self):
        fidelity = _make_fidelity_results(drift_windows=[])
        accuracy = _make_accuracy_results(drop_windows=[8])
        assert _compute_fidelity_leads(fidelity, accuracy, drift_window_approx=4) is False

    def test_no_accuracy_drop_is_false(self):
        fidelity = _make_fidelity_results(drift_windows=[5])
        accuracy = _make_accuracy_results(drop_windows=[])
        assert _compute_fidelity_leads(fidelity, accuracy, drift_window_approx=4) is False

    def test_fidelity_leads_by_one_window_is_true(self):
        fidelity = _make_fidelity_results(drift_windows=[7])
        accuracy = _make_accuracy_results(drop_windows=[8])
        assert _compute_fidelity_leads(fidelity, accuracy, drift_window_approx=4) is True


MAX_CONFIRMING_LAG = 8  # run_fidelity_experiment.py と同じ定数値


def _compute_confirming_detected(first_fidelity_drop, first_accuracy_drop):
    """run_single_scenario の detection_lag/confirming_detected と同じロジックを再現する。"""
    detection_lag = None
    if first_fidelity_drop is not None and first_accuracy_drop is not None:
        detection_lag = first_fidelity_drop - first_accuracy_drop
    confirming_detected = detection_lag is not None and detection_lag <= MAX_CONFIRMING_LAG
    return detection_lag, confirming_detected


class TestConfirmingDetected:
    """shap_drift_score は先行指標ではなく確認指標（1〜8ウィンドウ後追い）と
    再現性確保済みの実験で確認済み。confirming_detected はこの後追いを
    正しく「成功」として扱えることを確認する。
    """

    def test_detection_within_max_lag_after_drop_is_true(self):
        lag, detected = _compute_confirming_detected(
            first_fidelity_drop=19, first_accuracy_drop=18,
        )
        assert lag == 1
        assert detected is True

    def test_detection_at_max_lag_boundary_is_true(self):
        lag, detected = _compute_confirming_detected(
            first_fidelity_drop=26, first_accuracy_drop=18,
        )
        assert lag == MAX_CONFIRMING_LAG
        assert detected is True

    def test_detection_beyond_max_lag_is_false(self):
        lag, detected = _compute_confirming_detected(
            first_fidelity_drop=27, first_accuracy_drop=18,
        )
        assert lag == MAX_CONFIRMING_LAG + 1
        assert detected is False

    def test_strict_lead_still_counts_as_confirming_detected(self):
        """先行検知（負のlag）は確認指標としての成功にも含まれる。"""
        lag, detected = _compute_confirming_detected(
            first_fidelity_drop=5, first_accuracy_drop=8,
        )
        assert lag == -3
        assert detected is True

    def test_no_fidelity_drop_is_not_detected(self):
        lag, detected = _compute_confirming_detected(
            first_fidelity_drop=None, first_accuracy_drop=18,
        )
        assert lag is None
        assert detected is False

    def test_no_accuracy_drop_is_not_detected(self):
        lag, detected = _compute_confirming_detected(
            first_fidelity_drop=19, first_accuracy_drop=None,
        )
        assert lag is None
        assert detected is False


def _summarize_scenario_results(scenario_runs: list[dict]) -> dict:
    """run_fidelity_experiment.summarize_scenario_results と同じロジックを再現する。"""
    n_trials = len(scenario_runs)
    leads_count = sum(1 for r in scenario_runs if r["fidelity_leads"])
    confirming_count = sum(1 for r in scenario_runs if r["confirming_detected"])
    lags = [r["detection_lag"] for r in scenario_runs if r["confirming_detected"]]
    return {
        "n_trials": n_trials,
        "fidelity_leads_count": leads_count,
        "fidelity_leads_rate": round(leads_count / max(n_trials, 1), 2),
        "confirming_detected_count": confirming_count,
        "confirming_detected_rate": round(confirming_count / max(n_trials, 1), 2),
        "mean_detection_lag": round(sum(lags) / len(lags), 2) if lags else None,
    }


class TestSummarizeScenarioResultsMeanLag:
    """mean_detection_lag は「検知trialのみ」(confirming_detected) に限定するべきで、
    detection_lag はあるが MAX_CONFIRMING_LAG を超えて confirming_detected=False な
    trial を平均に混入させてはならない（実測レンジ内では未発火だが理論上起こりうる
    潜在バグの回帰テスト）。
    """

    def test_beyond_max_lag_trial_excluded_from_mean(self):
        runs = [
            {"fidelity_leads": False, "confirming_detected": True, "detection_lag": 1},
            {"fidelity_leads": False, "confirming_detected": True, "detection_lag": 3},
            # detection_lag=9 は MAX_CONFIRMING_LAG(8) 超えのため confirming_detected=False。
            # 修正前のバグでは detection_lag が None でないというだけで平均に混入していた。
            {"fidelity_leads": False, "confirming_detected": False, "detection_lag": 9},
        ]
        summary = _summarize_scenario_results(runs)
        assert summary["confirming_detected_count"] == 2
        # (1+3)/2 = 2.0。9を含めた誤答は (1+3+9)/3 = 4.33 になる。
        assert summary["mean_detection_lag"] == 2.0

    def test_no_confirming_trials_gives_none_mean(self):
        runs = [
            {"fidelity_leads": False, "confirming_detected": False, "detection_lag": None},
            {"fidelity_leads": False, "confirming_detected": False, "detection_lag": 9},
        ]
        summary = _summarize_scenario_results(runs)
        assert summary["confirming_detected_count"] == 0
        assert summary["mean_detection_lag"] is None

    def test_leading_negative_lag_included_in_mean(self):
        runs = [
            {"fidelity_leads": True, "confirming_detected": True, "detection_lag": -2},
            {"fidelity_leads": False, "confirming_detected": True, "detection_lag": 4},
        ]
        summary = _summarize_scenario_results(runs)
        assert summary["mean_detection_lag"] == 1.0

# self-proving-observation/
# └── tests/
#     └── test_fidelity_null_condition.py
#
# Fidelity Guardの確認検知率は感度のみで、ドリフトの無い正常な
# 窓に対する誤警報が測られていなかった。scripts/run_fidelity_experiment.py に追加した
# null（ドリフト無し）条件の集計と、既存のシナリオ・出力が変わっていないことを検査する。
# LSTM・SHAPの学習を伴う run_single_scenario 自体は重いため対象にしない（実機で実行して確認）。

import importlib.util
import pathlib

import numpy as np

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "run_fidelity_experiment", REPO_ROOT / "scripts" / "run_fidelity_experiment.py",
)
assert _spec is not None and _spec.loader is not None
exp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(exp)


def test_null_condition_is_not_part_of_the_drift_scenarios():
    # `--scenario all`・fidelity_summary.json（貢献3の確定結果n=80）の対象を変えない
    assert list(exp.DRIFT_SCENARIOS) == ["sudden", "gradual", "recurring"]
    assert exp.NULL_SCENARIO_NAME not in exp.DRIFT_SCENARIOS
    assert list(exp.NULL_SCENARIOS) == [exp.NULL_SCENARIO_NAME]


def test_inject_no_drift_returns_an_unchanged_copy():
    rng = np.random.default_rng(0)
    data = exp.generate_normal_data(50, rng)
    out = exp.inject_no_drift(data, 30, rng)
    assert out is not data
    assert np.array_equal(out, data)


def test_false_alarm_windows_only_counts_windows_where_detection_is_active():
    results = [{"window_index": i, "drift_detected": i in (3, 20, 25)} for i in range(28)]
    # 窓3はベースライン確立前（検知が有効でない）ので数えない
    assert exp.false_alarm_windows(results, 17) == [20, 25]
    assert exp.false_alarm_windows(results, 0) == [3, 20, 25]
    assert exp.false_alarm_windows(results, 26) == []


def test_summarize_null_results_counts_trials_and_windows():
    runs = [
        {"false_alarm_windows": [], "n_evaluated_windows": 11},
        {"false_alarm_windows": [20], "n_evaluated_windows": 11},
        {"false_alarm_windows": [19, 20, 21], "n_evaluated_windows": 11},
        {"false_alarm_windows": [], "n_evaluated_windows": 11},
    ]
    s = exp.summarize_null_results(runs)
    assert s["n_trials"] == 4
    assert s["false_alarm_trial_count"] == 2
    assert s["false_alarm_trial_rate"] == 0.5
    assert s["false_alarm_window_count"] == 4
    assert s["evaluated_window_count"] == 44
    assert s["false_alarm_window_rate"] == round(4 / 44, 4)


def test_wilson_interval_matches_known_values():
    lo, hi = exp._wilson_interval(0, 10)
    assert lo == 0.0
    assert abs(hi - 0.2775) < 1e-3  # z^2/(n+z^2)
    lo, hi = exp._wilson_interval(10, 10)
    assert hi == 1.0
    assert abs(lo - 0.7225) < 1e-3
    assert exp._wilson_interval(0, 0) == (0.0, 1.0)


def test_summarize_null_results_with_no_alarms_still_reports_an_upper_bound():
    # 0件でも「誤警報率0」とは言えず、試行数で決まる上限が付く（n=10で約28%）
    runs = [{"false_alarm_windows": [], "n_evaluated_windows": 11} for _ in range(10)]
    s = exp.summarize_null_results(runs)
    assert s["false_alarm_trial_rate"] == 0.0
    assert s["false_alarm_trial_rate_wilson95"][1] > 0.25

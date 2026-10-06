# self-proving-observation/
# └── tests/
#     └── test_stats.py

from src.measure.stats import (
    analyze_batch_results,
    analyze_condition,
    cochran_armitage_trend_test,
    fisher_exact_loss_occurrence,
    kruskal_wallis_test,
    trend_test_by_pattern,
)


def _make_trial(baseline_loss, baseline_failed=False, proposed_loss=0.0):
    return {
        "loss": {
            "proposed": {"loss_rate_percent": proposed_loss, "verification_failed": False},
            "baseline": {
                "loss_rate_percent": baseline_loss,
                "verification_failed": baseline_failed,
            },
        },
        "resources": {
            "proposed": {"cpu_peak": 10.0, "mem_peak": 5.0},
            "baseline": {"cpu_peak": 20.0, "mem_peak": 15.0},
        },
    }


class TestAnalyzeCondition:
    def test_excludes_verification_failed_from_loss_rate(self):
        """verification_failed: true の試行は loss_rate_percent の統計から除外される。

        ES 429等で検証クエリ自体が失敗した場合、loss_rate.pyはloss_rate_percent=100.0
        （UNVERIFIED）を記録するが、これを真の欠損としてMann-Whitney U検定に含めると
        統計的に有意な差（p<.001等）が誤って検出されうる（`aggregate.py`と同じ扱いに揃える）。
        """
        trials = [
            _make_trial(baseline_loss=0.0, baseline_failed=False),
            _make_trial(baseline_loss=100.0, baseline_failed=True),
        ]
        values = analyze_condition(trials, "baseline", "loss_rate_percent")
        assert values == [0.0]

    def test_resource_metrics_not_filtered_by_verification_failed(self):
        """verification_failedはloss_rateのみが対象で、リソース指標は影響を受けない。"""
        trials = [_make_trial(baseline_loss=100.0, baseline_failed=True)]
        values = analyze_condition(trials, "baseline", "cpu_peak")
        assert values == [20.0]


class TestAnalyzeBatchResults:
    def test_verification_failed_excluded_from_test_and_reported_separately(
        self, tmp_path, monkeypatch,
    ):
        trials = [
            _make_trial(baseline_loss=0.0, baseline_failed=False),
            _make_trial(baseline_loss=0.0, baseline_failed=False),
            _make_trial(baseline_loss=100.0, baseline_failed=True),
        ]

        monkeypatch.setattr(
            "src.measure.stats.scan_batch_dir", lambda batch_dir: ["run1", "run2", "run3"]
        )
        monkeypatch.setattr(
            "src.measure.stats.group_runs",
            lambda runs: {("spike", 1000): trials},
        )

        result = analyze_batch_results(str(tmp_path))
        loss = result["conditions"][0]["loss_rate"]

        assert loss["baseline_raw"] == [0.0, 0.0]
        assert loss["n_verification_failed"] == {"baseline": 1, "proposed": 0}

    def test_occurrence_test_included_alongside_continuous_test(self, tmp_path, monkeypatch):
        """occurrence_test（Fisher正確検定）が既存のtest（Mann-Whitney）と併記される。"""
        trials = [
            _make_trial(baseline_loss=0.0, baseline_failed=False),
            _make_trial(baseline_loss=5.0, baseline_failed=False),
        ]

        monkeypatch.setattr(
            "src.measure.stats.scan_batch_dir", lambda batch_dir: ["run1", "run2"]
        )
        monkeypatch.setattr(
            "src.measure.stats.group_runs",
            lambda runs: {("spike", 1000): trials},
        )

        result = analyze_batch_results(str(tmp_path))
        loss = result["conditions"][0]["loss_rate"]

        assert "occurrence_test" in loss
        assert loss["occurrence_test"]["baseline_loss_count"] == 1
        assert loss["occurrence_test"]["proposed_loss_count"] == 0


class TestFisherExactLossOccurrence:
    def test_proposed_always_zero_baseline_sometimes_loses(self):
        """proposedが常に0%・baselineが間欠的に欠損する典型ケース。"""
        baseline = [0.0, 0.0, 0.0, 4.47, 0.0, 3.2, 0.0, 0.0]
        proposed = [0.0] * 8

        result = fisher_exact_loss_occurrence(baseline, proposed)

        assert result["baseline_loss_count"] == 2
        assert result["proposed_loss_count"] == 0
        assert result["p_value"] is not None

    def test_empty_input_returns_none_p_value(self):
        result = fisher_exact_loss_occurrence([], [0.0, 1.0])

        assert result["p_value"] is None
        assert result["n_baseline"] == 0

    def test_no_losses_in_either_group(self):
        result = fisher_exact_loss_occurrence([0.0, 0.0], [0.0, 0.0])

        assert result["baseline_loss_count"] == 0
        assert result["proposed_loss_count"] == 0
        assert result["p_value"] == 1.0


class TestCochranArmitageTrendTest:
    def test_monotonic_increase_is_detected(self):
        """欠損発生率がRPS上昇とともに単調増加する場合、増加方向で有意になる。"""
        result = cochran_armitage_trend_test(
            rps_levels=[100, 500, 1000, 2000, 5000],
            ns=[20, 20, 20, 20, 20],
            xs=[0, 1, 3, 8, 15],
        )

        assert result["z_statistic"] is not None
        assert result["z_statistic"] > 0
        assert result["p_value_one_sided_increasing"] < 0.05

    def test_no_trend_when_rate_constant(self):
        result = cochran_armitage_trend_test(
            rps_levels=[100, 500, 1000, 2000, 5000],
            ns=[20, 20, 20, 20, 20],
            xs=[0, 0, 0, 0, 0],
        )

        assert result["z_statistic"] == 0.0
        assert result["p_value_two_sided"] == 1.0

    def test_fewer_than_three_levels_returns_none(self):
        result = cochran_armitage_trend_test(
            rps_levels=[100, 500], ns=[20, 20], xs=[0, 1],
        )

        assert result["z_statistic"] is None
        assert result["n_levels"] == 2

    def test_unsorted_input_is_sorted_by_rps(self):
        """rps_levelsが未整列でも内部でRPS昇順に並べ替えてから検定する。"""
        sorted_result = cochran_armitage_trend_test(
            rps_levels=[100, 500, 1000], ns=[20, 20, 20], xs=[0, 2, 6],
        )
        unsorted_result = cochran_armitage_trend_test(
            rps_levels=[1000, 100, 500], ns=[20, 20, 20], xs=[6, 0, 2],
        )

        assert unsorted_result["z_statistic"] == sorted_result["z_statistic"]
        assert unsorted_result["rps_levels"] == [100.0, 500.0, 1000.0]


class TestTrendTestByPattern:
    def test_groups_conditions_by_pattern_across_rps(self):
        conditions = [
            {
                "pattern": "ramp", "rps": rps,
                "loss_rate": {
                    "occurrence_test": {"n_baseline": 20, "baseline_loss_count": loss},
                },
            }
            for rps, loss in [(100, 0), (500, 1), (1000, 3), (2000, 8), (5000, 15)]
        ]

        result = trend_test_by_pattern(conditions)

        assert "ramp" in result
        assert result["ramp"]["n_levels"] == 5
        assert result["ramp"]["p_value_one_sided_increasing"] < 0.05

    def test_conditions_without_occurrence_test_are_skipped(self):
        conditions = [{"pattern": "flat", "rps": 100, "loss_rate": {}}]

        result = trend_test_by_pattern(conditions)

        assert result == {}


class TestKruskalWallisTest:
    def test_detects_difference_across_three_groups(self):
        groups = {
            "月": [100.0, 110.0, 105.0],
            "火": [200.0, 210.0, 205.0],
            "水": [300.0, 310.0, 305.0],
        }
        result = kruskal_wallis_test(groups)

        assert result["n_groups"] == 3
        assert result["p_value"] < 0.05
        assert result["group_n"] == {"月": 3, "火": 3, "水": 3}
        assert result["group_mean"]["月"] == 105.0

    def test_identical_groups_yield_high_p_value(self):
        groups = {"月": [100.0, 100.0], "火": [100.0, 100.0], "水": [100.0, 100.0]}
        result = kruskal_wallis_test(groups)

        assert result["p_value"] == 1.0

    def test_fewer_than_two_nonempty_groups_returns_none(self):
        groups = {"月": [100.0], "火": [], "水": []}
        result = kruskal_wallis_test(groups)

        assert result["H_statistic"] is None
        assert result["p_value"] is None
        assert result["n_groups"] == 1
        assert result["group_n"] == {"月": 1, "火": 0, "水": 0}

    def test_empty_groups_dict_returns_none(self):
        result = kruskal_wallis_test({})

        assert result["H_statistic"] is None
        assert result["n_groups"] == 0

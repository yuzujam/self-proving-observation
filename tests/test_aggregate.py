# self-proving-observation/
# └── tests/
#     └── test_aggregate.py

import csv
import math

import pytest

from src.measure.aggregate import (
    _parse_mem_mib,
    aggregate,
    aggregate_resource_totals,
    compute_stats,
    parse_resource_csv,
    parse_run_name,
    scan_batch_dir,
)


class TestComputeStats:
    def test_empty_returns_none(self):
        result = compute_stats([])
        assert result["mean"] is None
        assert result["std"] is None
        assert result["n"] == 0

    def test_single_value_std_zero(self):
        result = compute_stats([5.0])
        assert result["mean"] == 5.0
        assert result["std"] == 0.0
        assert result["n"] == 1

    def test_mean_is_correct(self):
        result = compute_stats([1.0, 2.0, 3.0, 4.0, 5.0])
        assert result["mean"] == 3.0

    def test_std_uses_ddof1(self):
        # n=5, values=[1,2,3,4,5], sample std = sqrt(10/4) = sqrt(2.5) ≈ 1.5811
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = compute_stats(values)
        expected_std = math.sqrt(sum((v - 3.0) ** 2 for v in values) / 4)
        assert abs(result["std"] - expected_std) < 1e-4

    def test_std_not_population(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = compute_stats(values)
        population_std = math.sqrt(sum((v - 3.0) ** 2 for v in values) / 5)
        assert result["std"] != pytest.approx(population_std, abs=1e-4)

    def test_min_max(self):
        result = compute_stats([3.0, 1.0, 4.0, 1.5, 9.0])
        assert result["min"] == 1.0
        assert result["max"] == 9.0


class TestParseResourceCsv:
    def _write_csv(self, path, rows, fieldnames=None):
        fieldnames = fieldnames or [
            "timestamp", "container", "cpu_percent", "mem_usage", "mem_percent", "block_io",
        ]
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_returns_stats_for_valid_csv(self, tmp_path):
        p = str(tmp_path / "resource.csv")
        self._write_csv(p, [
            {"timestamp": "t", "container": "c", "cpu_percent": "10.0",
             "mem_usage": "100MiB", "mem_percent": "20.0", "block_io": "0B"},
            {"timestamp": "t", "container": "c", "cpu_percent": "30.0",
             "mem_usage": "200MiB", "mem_percent": "40.0", "block_io": "0B"},
        ])
        result = parse_resource_csv(p)
        assert result["cpu_peak"] == 30.0
        assert result["mem_peak"] == 40.0
        assert result["cpu_mean"] == 20.0
        assert result["mem_mean"] == 30.0

    def test_returns_empty_when_cpu_values_missing(self, tmp_path):
        p = str(tmp_path / "resource.csv")
        self._write_csv(p, [
            {"timestamp": "t", "container": "c", "cpu_percent": "bad",
             "mem_usage": "100MiB", "mem_percent": "20.0", "block_io": "0B"},
        ])
        result = parse_resource_csv(p)
        assert result == {}

    def test_returns_empty_when_mem_values_missing(self, tmp_path):
        """BUG-22: cpu_values あり・mem_values なしのときクラッシュしない。"""
        p = str(tmp_path / "resource.csv")
        self._write_csv(p, [
            {"timestamp": "t", "container": "c", "cpu_percent": "10.0",
             "mem_usage": "100MiB", "mem_percent": "bad", "block_io": "0B"},
        ])
        result = parse_resource_csv(p)
        assert result == {}


class TestAggregate:
    def _make_run(self, pattern, rps, trial, proposed_loss, baseline_loss, baseline_failed=False):
        return {
            "pattern": pattern,
            "rps": rps,
            "trial": trial,
            "loss": {
                "proposed": {"loss_rate_percent": proposed_loss, "verification_failed": False},
                "baseline": {
                    "loss_rate_percent": baseline_loss,
                    "verification_failed": baseline_failed,
                },
            },
            "resources": {},
        }

    def test_excludes_verification_failed_from_loss_rate(self):
        """verification_failed: true の試行は loss_rate 集計から除外される。"""
        runs = [
            self._make_run("spike", 1000, 1, 0.0, 50.0, baseline_failed=False),
            self._make_run("spike", 1000, 2, 0.0, 100.0, baseline_failed=True),
        ]
        summary = aggregate(runs)
        cond = summary["conditions"][0]
        assert cond["baseline"]["loss_rate"]["mean"] == 50.0
        assert cond["baseline"]["n_verification_failed"] == 1
        assert cond["proposed"]["n_verification_failed"] == 0

    def test_all_failed_returns_none_mean(self):
        """全試行が verification_failed の場合は loss_rate が N/A になる。"""
        runs = [
            self._make_run("wave", 500, 1, 0.0, 100.0, baseline_failed=True),
            self._make_run("wave", 500, 2, 0.0, 100.0, baseline_failed=True),
        ]
        summary = aggregate(runs)
        cond = summary["conditions"][0]
        assert cond["baseline"]["loss_rate"]["mean"] is None
        assert cond["baseline"]["n_verification_failed"] == 2

    def test_n_verification_failed_in_output(self):
        """n_verification_failed フィールドが出力に含まれる。"""
        runs = [self._make_run("spike", 100, 1, 0.0, 0.0)]
        summary = aggregate(runs)
        cond = summary["conditions"][0]
        assert "n_verification_failed" in cond["baseline"]
        assert "n_verification_failed" in cond["proposed"]


class TestParseRunName:
    def test_valid_run_name(self):
        result = parse_run_name("spike_rps1000_trial3")
        assert result == {"pattern": "spike", "rps": 1000, "trial": 3}

    def test_flat_pattern(self):
        result = parse_run_name("flat_rps100_trial1")
        assert result["pattern"] == "flat"
        assert result["rps"] == 100

    def test_invalid_returns_none(self):
        assert parse_run_name("invalid") is None
        assert parse_run_name("spike_rps") is None
        assert parse_run_name("") is None


class TestParseMemMib:
    def test_mib(self):
        assert _parse_mem_mib("665.4MiB / 1GiB") == pytest.approx(665.4)

    def test_gib_converted_to_mib(self):
        assert _parse_mem_mib("1.867GiB / 2GiB") == pytest.approx(1.867 * 1024, rel=1e-6)

    def test_kib_converted_to_mib(self):
        assert _parse_mem_mib("512KiB / 1GiB") == pytest.approx(0.5, rel=1e-6)

    def test_bytes_converted_to_mib(self):
        assert _parse_mem_mib("1048576B / 1GiB") == pytest.approx(1.0, rel=1e-6)

    def test_unparseable_returns_none(self):
        assert _parse_mem_mib("garbage") is None


class TestAggregateResourceTotals:
    """doc/known-limitations.md #GGG: 6.5節の表（時刻ごとの全コンテナ合計%の
    平均・中央値・p95）を算出した手順がリポジトリに存在しなかった。"""

    @staticmethod
    def _write_resource_csv(path, rows):
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["timestamp", "container", "cpu_percent",
                            "mem_usage", "mem_percent", "block_io"],
            )
            writer.writeheader()
            for ts, container, cpu, mem_usage in rows:
                writer.writerow({
                    "timestamp": ts, "container": container, "cpu_percent": str(cpu),
                    "mem_usage": mem_usage, "mem_percent": "0", "block_io": "0B",
                })

    def test_sums_per_timestamp_across_containers(self, tmp_path):
        """同一時刻の複数コンテナのCPU%・メモリはtimestamp単位で合算される。"""
        p = str(tmp_path / "resource_proposed.csv")
        self._write_resource_csv(p, [
            ("t1", "proposed-receiver-1", 10.0, "100MiB / 1GiB"),
            ("t1", "proposed-worker-1", 20.0, "200MiB / 1GiB"),
            ("t2", "proposed-receiver-1", 30.0, "300MiB / 1GiB"),
            ("t2", "proposed-worker-1", 40.0, "400MiB / 1GiB"),
        ])
        result = aggregate_resource_totals([p], container_prefix="proposed-")
        assert result["n_timestamps"] == 2
        # CPU: t1合計=30, t2合計=70 → 平均50。メモリ: t1合計=300, t2合計=700 → 平均500
        assert result["cpu_percent_total"]["mean"] == 50.0
        assert result["mem_mib_total"]["mean"] == 500.0

    def test_ignores_containers_outside_prefix(self, tmp_path):
        """混入した無関係なコンテナ（同居T-Pot・逆スタック）は合計に含めない。"""
        p = str(tmp_path / "resource_baseline.csv")
        self._write_resource_csv(p, [
            ("t1", "baseline-elasticsearch-1", 10.0, "100MiB / 1GiB"),
            ("t1", "cowrie", 900.0, "50MiB / 1GiB"),
            ("t1", "proposed-vector-1", 500.0, "500MiB / 1GiB"),
        ])
        result = aggregate_resource_totals([p], container_prefix="baseline-")
        assert result["n_timestamps"] == 1
        assert result["cpu_percent_total"]["mean"] == 10.0
        assert result["mem_mib_total"]["mean"] == 100.0

    def test_pools_across_multiple_files_as_independent_samples(self, tmp_path):
        """複数ファイル（複数バッチ・試行）を渡すと、時刻系列はファイルごと独立にプールされる。"""
        p1 = str(tmp_path / "run1_resource_proposed.csv")
        p2 = str(tmp_path / "run2_resource_proposed.csv")
        self._write_resource_csv(p1, [("t1", "proposed-worker-1", 10.0, "10MiB / 1GiB")])
        self._write_resource_csv(p2, [("t1", "proposed-worker-1", 30.0, "30MiB / 1GiB")])
        result = aggregate_resource_totals([p1, p2], container_prefix="proposed-")
        assert result["n_timestamps"] == 2
        assert result["cpu_percent_total"]["mean"] == 20.0

    def test_no_matching_containers_returns_none_stats(self, tmp_path):
        """対象コンテナが1つもない場合、混入値で埋めずNoneを返す（内部指針 5.4）。"""
        p = str(tmp_path / "resource_baseline.csv")
        self._write_resource_csv(p, [("t1", "cowrie", 900.0, "50MiB / 1GiB")])
        result = aggregate_resource_totals([p], container_prefix="baseline-")
        assert result["n_timestamps"] == 0
        assert result["cpu_percent_total"]["mean"] is None
        assert result["mem_mib_total"]["mean"] is None


class TestScanBatchDirResourceFiltering:
    """doc/known-limitations.md #AA: monitor/resource.pyのSSH経由取得は同居する無関係な
    コンテナ（baseline-nodeの本番T-Potハニーポット群、逆側スタック等）も記録する。
    parse_resource_csvにcontainer_prefix引数が追加されただけで、scan_batch_dirが
    それを渡しておらず、stats.py・batch_report.py・pool_control_experiment_stats.py
    のCPU/メモリピークが混入した値のままだった（#TT）。"""

    @staticmethod
    def _write_resource_csv(path, rows):
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["timestamp", "container", "cpu_percent",
                            "mem_usage", "mem_percent", "block_io"],
            )
            writer.writeheader()
            for container, cpu, mem in rows:
                writer.writerow({
                    "timestamp": "t", "container": container, "cpu_percent": str(cpu),
                    "mem_usage": "1MiB", "mem_percent": str(mem), "block_io": "0B",
                })

    def test_resources_are_restricted_to_each_systems_own_stack(self, tmp_path):
        run_dir = tmp_path / "flat_rps100_trial1"
        run_dir.mkdir()
        # baselineのCSVには同居する本番T-Pot（cowrie等）と逆スタックのコンテナが混入する
        self._write_resource_csv(run_dir / "resource_baseline.csv", [
            ("baseline-elasticsearch-1", 40.0, 30.0),
            ("baseline-logstash-1", 20.0, 10.0),
            ("cowrie", 900.0, 95.0),
            ("proposed-vector-1", 500.0, 80.0),
        ])
        self._write_resource_csv(run_dir / "resource_proposed.csv", [
            ("proposed-receiver-1", 15.0, 5.0),
            ("proposed-worker-1", 25.0, 6.0),
            ("baseline-kibana-1", 700.0, 90.0),
        ])

        (run,) = scan_batch_dir(str(tmp_path))

        assert run["resources"]["baseline"]["cpu_peak"] == 40.0
        assert run["resources"]["baseline"]["mem_peak"] == 30.0
        assert run["resources"]["proposed"]["cpu_peak"] == 25.0
        assert run["resources"]["proposed"]["mem_peak"] == 6.0

    def test_stack_with_no_matching_containers_yields_no_resource_stats(self, tmp_path):
        # 他スタックのコンテナしか記録されていない場合、混入値で埋めずに空とする
        # （欠測を「別コンテナの値」で代替しない、内部指針 5.4）。
        run_dir = tmp_path / "flat_rps100_trial1"
        run_dir.mkdir()
        self._write_resource_csv(run_dir / "resource_baseline.csv", [
            ("cowrie", 900.0, 95.0),
        ])

        (run,) = scan_batch_dir(str(tmp_path))

        assert run["resources"]["baseline"] == {}

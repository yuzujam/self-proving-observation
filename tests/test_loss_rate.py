# self-proving-observation/
# └── tests/
#     └── test_loss_rate.py

import csv
import json
from urllib.error import HTTPError

from src.measure.loss_rate import _host_of, load_inject_log, measure_loss, query_elasticsearch_ids


class TestHostOf:
    def test_full_url_returns_hostname(self):
        assert _host_of("http://203.0.113.10:8123") == "203.0.113.10"

    def test_netloc_without_scheme_returns_host(self):
        assert _host_of("203.0.113.10:8000") == "203.0.113.10"

    def test_url_with_path_returns_hostname(self):
        assert _host_of("http://203.0.113.10:8000/events") == "203.0.113.10"

    def test_localhost_url(self):
        assert _host_of("http://localhost:8123") == "localhost"

    def test_localhost_netloc(self):
        assert _host_of("localhost:8000") == "localhost"

    def test_named_host(self):
        assert _host_of("http://myhost.example.com:9200") == "myhost.example.com"


class TestLoadInjectLog:
    def _write_csv(self, rows: list[dict], path: str):
        fieldnames = ["inject_id", "timestamp", "event_type", "target", "sent_ok"]
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_basic_load(self, tmp_path):
        p = str(tmp_path / "inject_log.csv")
        self._write_csv([
            {"inject_id": "id-1", "timestamp": "2026-01-01T00:00:00", "event_type": "alert",
             "target": "192.168.1.1:8000", "sent_ok": "1"},
        ], p)
        target_ids, ts_min, ts_max = load_inject_log(p)
        assert "192.168.1.1:8000" in target_ids
        assert "id-1" in target_ids["192.168.1.1:8000"]

    def test_sent_ok_0_is_excluded(self, tmp_path):
        p = str(tmp_path / "inject_log.csv")
        self._write_csv([
            {"inject_id": "id-ok", "timestamp": "2026-01-01T00:00:00", "event_type": "alert",
             "target": "host:8000", "sent_ok": "1"},
            {"inject_id": "id-fail", "timestamp": "2026-01-01T00:00:01", "event_type": "alert",
             "target": "host:8000", "sent_ok": "0"},
        ], p)
        target_ids, _, _ = load_inject_log(p)
        assert "id-ok" in target_ids["host:8000"]
        assert "id-fail" not in target_ids["host:8000"]

    def test_missing_sent_ok_column_includes_all(self, tmp_path):
        p = str(tmp_path / "inject_log.csv")
        with open(p, "w", newline="") as f:
            fieldnames = ["inject_id", "timestamp", "event_type", "target"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({"inject_id": "id-x", "timestamp": "2026-01-01T00:00:00",
                             "event_type": "alert", "target": "host:8000"})
        target_ids, _, _ = load_inject_log(p)
        assert "id-x" in target_ids["host:8000"]

    def test_timestamp_range(self, tmp_path):
        p = str(tmp_path / "inject_log.csv")
        self._write_csv([
            {"inject_id": "id-a", "timestamp": "2026-01-01T00:00:00", "event_type": "alert",
             "target": "host:8000", "sent_ok": "1"},
            {"inject_id": "id-b", "timestamp": "2026-01-01T01:00:00", "event_type": "alert",
             "target": "host:8000", "sent_ok": "1"},
        ], p)
        _, ts_min, ts_max = load_inject_log(p)
        assert ts_min == "2026-01-01T00:00:00"
        assert ts_max == "2026-01-01T01:00:00"

    def test_multiple_targets_separated(self, tmp_path):
        p = str(tmp_path / "inject_log.csv")
        self._write_csv([
            {"inject_id": "p-1", "timestamp": "2026-01-01T00:00:00", "event_type": "alert",
             "target": "proposed:8000", "sent_ok": "1"},
            {"inject_id": "b-1", "timestamp": "2026-01-01T00:00:00", "event_type": "alert",
             "target": "baseline:5080", "sent_ok": "1"},
        ], p)
        target_ids, _, _ = load_inject_log(p)
        assert "proposed:8000" in target_ids
        assert "baseline:5080" in target_ids
        assert "p-1" in target_ids["proposed:8000"]
        assert "b-1" in target_ids["baseline:5080"]


class TestMeasureLoss:
    def _write_inject_log(self, path: str, proposed_ids: list[str], baseline_ids: list[str]):
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["inject_id", "timestamp", "event_type", "target", "sent_ok"])
            for i in proposed_ids:
                writer.writerow([i, "2026-01-01T00:00:00", "alert", "proposed:8000", "1"])
            for i in baseline_ids:
                writer.writerow([i, "2026-01-01T00:00:00", "alert", "baseline:5080", "1"])

    def test_loss_rate_zero_when_all_recorded(self, tmp_path, monkeypatch):
        inject_log = str(tmp_path / "inject_log.csv")
        self._write_inject_log(inject_log, ["p-1", "p-2"], ["b-1", "b-2"])
        output = str(tmp_path / "loss_rate.json")

        monkeypatch.setattr(
            "src.measure.loss_rate.query_clickhouse_ids",
            lambda *a, **kw: {"p-1", "p-2"},
        )
        monkeypatch.setattr(
            "src.measure.loss_rate.query_elasticsearch_ids",
            lambda *a, **kw: {"b-1", "b-2"},
        )

        results = measure_loss(
            inject_log,
            clickhouse_url="http://proposed:8123",
            elasticsearch_url="http://baseline:9200",
            output_path=output,
            proposed_target="http://proposed:8000/events",
            baseline_target="http://baseline:5080",
        )

        assert results["proposed"]["loss_rate_percent"] == 0.0
        assert results["proposed"]["total_injected"] == 2
        assert results["baseline"]["loss_rate_percent"] == 0.0
        assert results["baseline"]["total_injected"] == 2

    def test_loss_rate_correct_when_some_missing(self, tmp_path, monkeypatch):
        inject_log = str(tmp_path / "inject_log.csv")
        self._write_inject_log(inject_log, ["p-1", "p-2", "p-3", "p-4"], [])
        output = str(tmp_path / "loss_rate.json")

        monkeypatch.setattr(
            "src.measure.loss_rate.query_clickhouse_ids",
            lambda *a, **kw: {"p-1", "p-2"},
        )
        monkeypatch.setattr(
            "src.measure.loss_rate.query_elasticsearch_ids",
            lambda *a, **kw: set(),
        )

        results = measure_loss(
            inject_log,
            clickhouse_url="http://proposed:8123",
            output_path=output,
            proposed_target="http://proposed:8000/events",
        )

        assert results["proposed"]["total_injected"] == 4
        assert results["proposed"]["total_recorded"] == 2
        assert results["proposed"]["total_lost"] == 2
        assert results["proposed"]["loss_rate_percent"] == 50.0


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _empty_agg_response():
    return _FakeResponse({"aggregations": {"unique_ids": {"buckets": []}}})


def _http_429():
    return HTTPError(url="http://es/_search", code=429, msg="Too Many Requests", hdrs=None, fp=None)


class TestQueryElasticsearchIdsRetry:
    """429（Too Many Requests）に対する指数バックオフ・リトライの挙動。"""

    def test_retries_and_succeeds_after_transient_429(self, monkeypatch):
        monkeypatch.setattr("src.measure.loss_rate.time.sleep", lambda *_: None)
        calls = {"n": 0}

        def fake_urlopen(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_429()
            return _empty_agg_response()

        monkeypatch.setattr("src.measure.loss_rate.urlopen", fake_urlopen)
        query_failed = []
        result = query_elasticsearch_ids("http://es", query_failed=query_failed)

        assert calls["n"] == 3
        assert result == set()
        assert query_failed == []

    def test_gives_up_after_max_attempts_and_marks_failed(self, monkeypatch):
        monkeypatch.setattr("src.measure.loss_rate.time.sleep", lambda *_: None)
        calls = {"n": 0}

        def fake_urlopen(*_args, **_kwargs):
            calls["n"] += 1
            raise _http_429()

        monkeypatch.setattr("src.measure.loss_rate.urlopen", fake_urlopen)
        query_failed = []
        result = query_elasticsearch_ids("http://es", query_failed=query_failed)

        assert calls["n"] == 4  # ES_RETRY_MAX_ATTEMPTS
        assert result == set()
        assert query_failed == [True]

    def test_non_429_http_error_fails_immediately_without_retry(self, monkeypatch):
        monkeypatch.setattr("src.measure.loss_rate.time.sleep", lambda *_: None)
        calls = {"n": 0}

        def fake_urlopen(*_args, **_kwargs):
            calls["n"] += 1
            raise HTTPError(url="http://es/_search", code=500, msg="Internal Server Error",
                             hdrs=None, fp=None)

        monkeypatch.setattr("src.measure.loss_rate.urlopen", fake_urlopen)
        query_failed = []
        result = query_elasticsearch_ids("http://es", query_failed=query_failed)

        assert calls["n"] == 1
        assert result == set()
        assert query_failed == [True]


class TestQueryClickhouseIdsTimeRange:
    """#VV: 時間範囲のオフセット付き値はUTCへ変換する（捨てて時刻をずらさない）。"""

    @staticmethod
    def _captured_params(monkeypatch, ts_from, ts_to):
        from urllib.parse import parse_qs, urlparse

        from src.measure.loss_rate import query_clickhouse_ids

        captured = {}

        def fake_request(url, body, *, headers=None, timeout=10):
            captured["params"] = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            return b""

        monkeypatch.setattr("src.measure.loss_rate.clickhouse_request", fake_request)
        query_clickhouse_ids("http://fake:8123", ts_from, ts_to)
        return captured["params"]

    def test_utc_timestamps_are_unchanged(self, monkeypatch):
        params = self._captured_params(
            monkeypatch, "2026-09-24T10:00:00.123456+00:00", "2026-09-24T10:02:00+00:00",
        )
        assert params["param_ts_from"] == "2026-09-24 10:00:00"
        assert params["param_ts_to"] == "2026-09-24 10:12:00"  # +10分バッファ

    def test_offset_timestamps_are_converted_to_utc(self, monkeypatch):
        params = self._captured_params(
            monkeypatch, "2026-09-24T12:00:00+02:00", "2026-09-24T12:02:00+02:00",
        )
        assert params["param_ts_from"] == "2026-09-24 10:00:00"
        assert params["param_ts_to"] == "2026-09-24 10:12:00"

    def test_naive_timestamps_are_treated_as_utc(self, monkeypatch):
        params = self._captured_params(monkeypatch, "2026-09-24T10:00:00", "2026-09-24T10:02:00")
        assert params["param_ts_from"] == "2026-09-24 10:00:00"

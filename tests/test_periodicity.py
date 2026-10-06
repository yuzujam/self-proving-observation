# self-proving-observation/
# └── tests/
#     └── test_periodicity.py

from src.measure.periodicity import (
    KNOWN_CONTAMINATED_SENSOR_IDS,
    UNSALVAGEABLE_END,
    UNSALVAGEABLE_START,
    fetch_clean_daily_counts,
    fetch_known_node_ids,
    group_by_cycle_phase,
    group_by_weekday,
)


class TestFetchKnownNodeIds:
    def test_returns_ips_from_known_nodes_table(self, monkeypatch):
        def fake_query_json(url, sql, params, timeout=10):
            return [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]

        monkeypatch.setattr("src.measure.periodicity.query_json", fake_query_json)

        assert fetch_known_node_ids("http://localhost:8123") == ("10.0.0.1", "10.0.0.2")

    def test_query_sent_to_clickhouse_has_exactly_one_format_clause(self, monkeypatch):
        """query_jsonが末尾にFORMAT JSONを付与するため、呼び出し側が付けると
        "FORMAT JSON FORMAT JSON"になりClickHouseが構文エラーを返す。従来のテストは
        query_json自体をモックしており、この二重付与を検出できなかった。"""
        captured = {}

        def fake_request(url, body, *, headers=None, timeout=10):
            captured["body"] = body.decode()
            return b'{"data": [{"ip": "10.0.0.1"}]}'

        monkeypatch.setattr("src.common.clickhouse.clickhouse_request", fake_request)

        assert fetch_known_node_ids("http://localhost:8123") == ("10.0.0.1",)
        assert captured["body"].upper().count("FORMAT") == 1
        assert captured["body"].endswith("FORMAT JSON")

    def test_falls_back_to_hardcoded_ids_on_query_error(self, monkeypatch):
        from urllib.error import URLError

        def fake_query_json(url, sql, params, timeout=10):
            raise URLError("connection refused")

        monkeypatch.setattr("src.measure.periodicity.query_json", fake_query_json)

        assert fetch_known_node_ids("http://localhost:8123") == KNOWN_CONTAMINATED_SENSOR_IDS

    def test_falls_back_to_hardcoded_ids_when_table_empty(self, monkeypatch):
        def fake_query_json(url, sql, params, timeout=10):
            return []

        monkeypatch.setattr("src.measure.periodicity.query_json", fake_query_json)

        assert fetch_known_node_ids("http://localhost:8123") == KNOWN_CONTAMINATED_SENSOR_IDS


class TestFetchCleanDailyCounts:
    def test_returns_parsed_rows(self, monkeypatch):
        captured = {}

        def fake_query_json(url, sql, params, timeout=10):
            captured["url"] = url
            captured["sql"] = sql
            captured["params"] = params
            captured["timeout"] = timeout
            return [
                {"day": "2026-07-13", "total_count": "468703"},
                {"day": "2026-07-14", "total_count": "422760"},
            ]

        monkeypatch.setattr("src.measure.periodicity.query_json", fake_query_json)

        result = fetch_clean_daily_counts(
            "http://localhost:8123", "2026-07-01 00:00:00", "2026-08-12 00:00:00",
        )

        assert result == [
            {"day": "2026-07-13", "total_count": 468703},
            {"day": "2026-07-14", "total_count": 422760},
        ]
        assert captured["url"] == "http://localhost:8123"
        assert captured["timeout"] == 120

    def test_where_clause_excludes_inject_id_and_known_sensors(self, monkeypatch):
        captured = {}

        def fake_query_json(url, sql, params, timeout=10):
            captured["sql"] = sql
            captured["params"] = params
            return []

        monkeypatch.setattr("src.measure.periodicity.query_json", fake_query_json)

        fetch_clean_daily_counts("http://localhost:8123", "2026-07-01", "2026-08-12")

        sql = captured["sql"]
        params = captured["params"]
        assert "inject_id = ''" in sql
        assert "sensor_id NOT IN" in sql
        assert params["known_sensors"] == list(KNOWN_CONTAMINATED_SENSOR_IDS)

    def test_where_clause_excludes_both_empty_and_unknown_sensor_ids(self, monkeypatch):
        """vector.toml修正（2026-08-13）後、src_ip未解決は空文字ではなく"unknown"で
        記録される。threat_events_clean VIEW（proposed/init.sql）と同じく両方を除外する。"""
        captured = {}

        def fake_query_json(url, sql, params, timeout=10):
            captured["sql"] = sql
            captured["params"] = params
            return []

        monkeypatch.setattr("src.measure.periodicity.query_json", fake_query_json)

        fetch_clean_daily_counts("http://localhost:8123", "2026-07-01", "2026-08-12")

        assert "sensor_id != ''" not in captured["sql"]
        assert "sensor_id NOT IN ({unresolved_sensors:Array(String)})" in captured["sql"]
        assert captured["params"]["unresolved_sensors"] == ["", "unknown"]

    def test_contaminated_sensor_ids_override_replaces_default(self, monkeypatch):
        """known_nodes由来のID一覧を渡せる（非破壊的拡張）。"""
        captured = {}

        def fake_query_json(url, sql, params, timeout=10):
            captured["params"] = params
            return []

        monkeypatch.setattr("src.measure.periodicity.query_json", fake_query_json)

        fetch_clean_daily_counts(
            "http://localhost:8123", "2026-07-01", "2026-08-12",
            contaminated_sensor_ids=("10.0.0.1", "10.0.0.2", "10.0.0.3"),
        )

        assert captured["params"]["known_sensors"] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    def test_exclude_unsalvageable_true_by_default(self, monkeypatch):
        captured = {}

        def fake_query_json(url, sql, params, timeout=10):
            captured["sql"] = sql
            captured["params"] = params
            return []

        monkeypatch.setattr("src.measure.periodicity.query_json", fake_query_json)

        fetch_clean_daily_counts("http://localhost:8123", "2026-07-01", "2026-08-12")

        assert "unsalvageable_start" in captured["sql"]
        assert captured["params"]["unsalvageable_start"] == UNSALVAGEABLE_START
        assert captured["params"]["unsalvageable_end"] == UNSALVAGEABLE_END

    def test_exclude_unsalvageable_false_omits_date_filter(self, monkeypatch):
        captured = {}

        def fake_query_json(url, sql, params, timeout=10):
            captured["sql"] = sql
            captured["params"] = params
            return []

        monkeypatch.setattr("src.measure.periodicity.query_json", fake_query_json)

        fetch_clean_daily_counts(
            "http://localhost:8123", "2026-07-01", "2026-08-12",
            exclude_unsalvageable=False,
        )

        assert "unsalvageable_start" not in captured["sql"]
        assert "unsalvageable_start" not in captured["params"]

    def test_clickhouse_error_returns_empty_list(self, monkeypatch):
        from urllib.error import URLError

        def fake_query_json(url, sql, params, timeout=10):
            raise URLError("connection refused")

        monkeypatch.setattr("src.measure.periodicity.query_json", fake_query_json)

        result = fetch_clean_daily_counts("http://localhost:8123", "2026-07-01", "2026-08-12")

        assert result == []


class TestGroupByWeekday:
    def test_groups_into_correct_weekday_buckets(self):
        # 2026-07-01は水曜、2026-07-06は月曜（date.fromisoformat基準で確認済み）
        daily_counts = [
            {"day": "2026-07-01", "total_count": 100},
            {"day": "2026-07-06", "total_count": 200},
            {"day": "2026-07-08", "total_count": 300},
        ]
        result = group_by_weekday(daily_counts)

        assert result["水"] == [100.0, 300.0]
        assert result["月"] == [200.0]
        assert result["火"] == []

    def test_returns_all_seven_labels_even_when_empty(self):
        result = group_by_weekday([])
        assert set(result.keys()) == {"月", "火", "水", "木", "金", "土", "日"}
        assert all(v == [] for v in result.values())


class TestGroupByCyclePhase:
    def test_bins_by_offset_from_first_day(self):
        daily_counts = [
            {"day": "2026-07-01", "total_count": 100},
            {"day": "2026-07-02", "total_count": 200},
            {"day": "2026-07-25", "total_count": 300},  # 07-01から24日後 → 位相0
        ]
        result = group_by_cycle_phase(daily_counts, period_days=24)

        assert result["0"] == [100.0, 300.0]
        assert result["1"] == [200.0]

    def test_first_day_determined_by_min_not_input_order(self):
        daily_counts = [
            {"day": "2026-07-03", "total_count": 300},
            {"day": "2026-07-01", "total_count": 100},
        ]
        result = group_by_cycle_phase(daily_counts, period_days=24)

        assert result["0"] == [100.0]
        assert result["2"] == [300.0]

    def test_empty_input_returns_empty_dict(self):
        assert group_by_cycle_phase([], period_days=24) == {}

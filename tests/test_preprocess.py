# self-proving-observation/
# └── tests/
#     └── test_preprocess.py

import numpy as np
import pytest

from src.ml.preprocess import create_sequences, normalize


class TestNormalize:
    def test_output_shape_unchanged(self):
        data = np.random.default_rng(0).random((50, 10)).astype(np.float32)
        normed, _, _ = normalize(data)
        assert normed.shape == data.shape

    def test_mean_near_zero(self):
        data = np.random.default_rng(0).random((100, 5)).astype(np.float32)
        normed, _, _ = normalize(data)
        assert np.abs(normed.mean(axis=0)).max() < 1e-5

    def test_std_near_one(self):
        data = np.random.default_rng(0).random((100, 5)).astype(np.float32)
        normed, _, _ = normalize(data)
        assert np.abs(normed.std(axis=0) - 1.0).max() < 1e-4

    def test_zero_std_column_not_nan(self):
        data = np.ones((10, 3), dtype=np.float32)
        normed, mean, std = normalize(data)
        assert not np.any(np.isnan(normed))
        assert np.all(std == 1.0)

    def test_returns_mean_std(self):
        data = np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]], dtype=np.float32)
        _, mean, std = normalize(data)
        np.testing.assert_allclose(mean, [3.0, 4.0], atol=1e-5)


class TestCreateSequences:
    def test_output_shapes(self):
        features = np.random.default_rng(0).random((20, 5)).astype(np.float32)
        X, y = create_sequences(features, seq_len=4)
        assert X.shape == (16, 4, 5)
        assert y.shape == (16, 5)

    def test_sequence_content(self):
        features = np.arange(30, dtype=np.float32).reshape(10, 3)
        X, y = create_sequences(features, seq_len=3)
        np.testing.assert_array_equal(X[0], features[0:3])
        np.testing.assert_array_equal(y[0], features[3])

    def test_insufficient_data_raises(self):
        features = np.ones((5, 3), dtype=np.float32)
        with pytest.raises(ValueError, match="Not enough data"):
            create_sequences(features, seq_len=5)

    def test_n_sequences_is_len_minus_seqlen(self):
        features = np.ones((100, 5), dtype=np.float32)
        X, y = create_sequences(features, seq_len=12)
        assert len(X) == 88
        assert len(y) == 88


class TestMissingWindowDetection:
    """#AAA: GROUP BYは空の窓を出力しないため、観測の空白が黙って詰められる。
    値は補間せず、欠けた窓の件数を数えて警告できるようにする。"""

    def test_no_gap_is_zero(self):
        from src.ml.preprocess import _count_missing_windows

        rows = [{"window_start": f"2026-09-24 10:{m:02d}:00"} for m in (0, 5, 10)]
        assert _count_missing_windows(rows) == 0

    def test_counts_each_missing_five_minute_window(self):
        from src.ml.preprocess import _count_missing_windows

        # 10:05 と 10:10 が欠け、さらに 10:30 が欠けている
        rows = [
            {"window_start": "2026-09-24 10:00:00"},
            {"window_start": "2026-09-24 10:15:00"},
            {"window_start": "2026-09-24 10:25:00"},
            {"window_start": "2026-09-24 10:35:00"},
        ]
        assert _count_missing_windows(rows) == 2 + 1 + 1

    def test_extract_still_returns_unfilled_rows_and_does_not_interpolate(self, monkeypatch):
        import numpy as np

        from src.ml import preprocess

        def fake_query(sql, params=None):
            base = {k: 1 for k in (
                "event_count", "unique_sensors", "avg_severity", "max_severity",
                "alert_count", "dns_count", "http_count", "tls_count", "flow_count", "ssh_count",
            )}
            return [
                {"window_start": "2026-09-24 10:00:00", **base},
                {"window_start": "2026-09-24 10:20:00", **base},
            ]

        monkeypatch.setattr(preprocess, "query_clickhouse", fake_query)

        features = preprocess.extract_windowed_features(1)

        assert features.shape == (2, 10)  # 欠けた窓を0埋め・補間で足さない
        assert features.dtype == np.float32

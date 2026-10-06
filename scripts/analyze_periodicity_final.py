# self-proving-observation/
# └── scripts/
#     └── analyze_periodicity_final.py — 6.3章の正式分析
#         （事前登録手順の実行、手順の事前登録は2026-09-30）

"""S3-compatible object storageの日次Parquetから清浄日次件数を集計し、事前登録した検定を実行する。

使い方:
    rclone copy "<rclone-remote>:<bucket>/clickhouse/threat_events/" <dir> \
        --include "threat_events_2026-07-0[3-6].parquet" ...
    （07-07〜07-12は取得しない。83日分のParquetが必要）
    python scripts/analyze_periodicity_final.py <dir> [--out result.json]

手順は事前登録どおりで、結果を見てからの変更は行わない（境界07-20固定・乱数シード42）。
対話セッション向けの解析スクリプト（`pool_control_experiment_stats.py`と同じ位置づけ）。
"""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow.compute as pc
import pyarrow.parquet as pq
from scipy import signal
from scipy import stats as sp_stats

KNOWN_IDS = ("203.0.113.10", "203.0.113.11")
UNRESOLVED_IDS = ("", "unknown")
FIRST_DAY = date(2026, 7, 3)
LAST_DAY = date(2026, 9, 29)
UNSALVAGEABLE = (date(2026, 7, 7), date(2026, 7, 12))  # 両端含む
LATE_START = date(2026, 7, 20)
OOO_DAYS = [date(2026, 7, d) for d in (22, 24, 25, 26, 28, 29, 31)]
DDD_DAY = date(2026, 9, 10)
N_PERM = 10_000
SEED = 42
ALPHA = 0.05
WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")

FloatArr = npt.NDArray[np.float64]


def load_daily_counts(data_dir: Path) -> dict[date, int]:
    """3条件除外後の日次sum(count)。対象は07-03〜09-29、07-07〜07-12は除外。"""
    out: dict[date, int] = {}
    excluded = list(KNOWN_IDS + UNRESOLVED_IDS)
    for d in range((LAST_DAY - FIRST_DAY).days + 1):
        day = FIRST_DAY + timedelta(days=d)
        if UNSALVAGEABLE[0] <= day <= UNSALVAGEABLE[1]:
            continue
        path = data_dir / f"threat_events_{day.isoformat()}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"{path} がない（欠日を黙って飛ばさない）")
        table = pq.read_table(path, columns=["timestamp", "sensor_id", "count", "inject_id"])
        mask = pc.and_(
            pc.equal(table["inject_id"], ""),
            pc.invert(pc.is_in(table["sensor_id"], value_set=_arr(excluded))),
        )
        table = table.filter(mask)
        # toDate(timestamp)はClickHouseセッションTZ=UTCの暦日
        days = pc.cast(table["timestamp"], "date32")
        day_total: dict[date, int] = {}
        for dd, cnt in zip(days.to_pylist(), table["count"].to_pylist(), strict=True):
            day_total[dd] = day_total.get(dd, 0) + int(cnt)
        out[day] = day_total.get(day, 0)
        stray = {k: v for k, v in day_total.items() if k != day}
        if stray:
            raise ValueError(f"{path.name} に他日の行がある: {sorted(stray)}")
    return out


def _arr(values: list[str]) -> Any:
    import pyarrow as pa

    return pa.array(values, type=pa.string())


def detrend_linear(y: FloatArr) -> FloatArr:
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    return y - (slope * x + intercept)


def detrend_rolling_median(y: FloatArr, window: int = 15) -> FloatArr:
    """15日中心移動中央値を引く。端は窓を縮める（行数の足りない端は対称に縮小）。"""
    half = window // 2
    med = np.empty_like(y)
    for i in range(len(y)):
        h = min(half, i, len(y) - 1 - i)
        med[i] = np.median(y[i - h : i + h + 1])
    return y - med


def kw_perm(
    resid: FloatArr, weekdays: npt.NDArray[np.int_], rng: np.random.Generator
) -> dict[str, Any]:
    def h_stat(labels: npt.NDArray[np.int_]) -> float:
        groups = [resid[labels == k] for k in range(7) if np.any(labels == k)]
        return float(sp_stats.kruskal(*groups).statistic)

    obs = h_stat(weekdays)
    ge = 0
    labels = weekdays.copy()
    for _ in range(N_PERM):
        rng.shuffle(labels)
        if h_stat(labels) >= obs:
            ge += 1
    n = len(resid)
    return {"H": obs, "eps2": obs / (n - 1), "p": (ge + 1) / (N_PERM + 1)}


def band_power(y: FloatArr, period: float, half_width: float) -> float:
    """周期band内のLomb-Scargleパワーの最大値（等間隔・日単位）。"""
    t = np.arange(len(y), dtype=float)
    periods = np.linspace(period - half_width, period + half_width, 41)
    ang = 2 * np.pi / periods
    y0 = y - y.mean()
    pgram = signal.lombscargle(t, y0, ang, normalize=True)
    return float(pgram.max())


def periodicity_test(
    resid: FloatArr, period: float, half_width: float, rng: np.random.Generator
) -> dict[str, Any]:
    obs = band_power(resid, period, half_width)
    n = len(resid)
    # (a) 並べ替え
    ge_a = sum(band_power(rng.permutation(resid), period, half_width) >= obs for _ in range(N_PERM))
    # (b) AR(1)代理系列
    r0 = resid - resid.mean()
    phi = float(np.dot(r0[1:], r0[:-1]) / np.dot(r0[:-1], r0[:-1]))
    sigma = float(np.std(r0[1:] - phi * r0[:-1], ddof=1))
    ge_b = 0
    burn = 100
    for _ in range(N_PERM):
        e = rng.normal(0.0, sigma, n + burn)
        s = np.empty(n + burn)
        s[0] = e[0]
        for i in range(1, n + burn):
            s[i] = phi * s[i - 1] + e[i]
        if band_power(s[burn:], period, half_width) >= obs:
            ge_b += 1
    p_a = (ge_a + 1) / (N_PERM + 1)
    p_b = (ge_b + 1) / (N_PERM + 1)
    return {"power": obs, "p_perm": p_a, "p_ar1": p_b, "p": max(p_a, p_b), "ar1_phi": phi}


def holm(ps: list[float]) -> list[float]:
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * ps[i]))
        adj[i] = running
    return adj


def analyze_late(
    counts: dict[date, int], drop: list[date], detrend: str
) -> dict[str, Any]:
    days = [d for d in sorted(counts) if d >= LATE_START and d not in drop]
    y = np.log(np.array([counts[d] for d in days], dtype=float))
    resid = detrend_linear(y) if detrend == "linear" else detrend_rolling_median(y)
    wd = np.array([d.weekday() for d in days])
    res_kw = kw_perm(resid, wd, np.random.default_rng(SEED))
    res_7 = periodicity_test(resid, 7.0, 0.5, np.random.default_rng(SEED))
    res_24 = periodicity_test(resid, 24.0, 1.0, np.random.default_rng(SEED))
    adj = holm([res_kw["p"], res_7["p"], res_24["p"]])
    res_kw["p_holm"], res_7["p_holm"], res_24["p_holm"] = adj
    claim = any(
        a < ALPHA and (name != "weekday" or res_kw["eps2"] >= 0.14)
        for a, name in zip(adj, ("weekday", "7d", "24d"), strict=True)
    )
    return {
        "n_days": len(days),
        "weekday": res_kw,
        "period_7d": res_7,
        "period_24d": res_24,
        "claim_periodicity": claim,
    }


def describe(counts: dict[date, int]) -> dict[str, Any]:
    early = [counts[d] for d in sorted(counts) if d < LATE_START]
    late_days = [d for d in sorted(counts) if d >= LATE_START]
    late = [counts[d] for d in late_days]
    by_wd: dict[str, float] = {}
    for k, label in enumerate(WEEKDAYS):
        vals = [counts[d] for d in late_days if d.weekday() == k]
        by_wd[label] = float(np.median(vals))
    q = lambda v: [float(x) for x in np.percentile(v, [25, 50, 75])]  # noqa: E731
    return {
        "n_early": len(early),
        "n_late": len(late),
        "early_q25_med_q75": q(early),
        "late_q25_med_q75": q(late),
        "late_over_early_median": float(np.median(late) / np.median(early)),
        "late_weekday_median": by_wd,
        "max_day": max(((d.isoformat(), counts[d]) for d in counts), key=lambda x: x[1]),
        "min_day": min(((d.isoformat(), counts[d]) for d in counts), key=lambda x: x[1]),
        "daily": {d.isoformat(): counts[d] for d in sorted(counts)},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    counts = load_daily_counts(args.data_dir)
    if len(counts) != 83:
        raise SystemExit(f"対象日数が83でない: {len(counts)}")
    result: dict[str, Any] = {"descriptive": describe(counts)}
    result["main"] = analyze_late(counts, [], "linear")
    result["S1_drop_OOO"] = analyze_late(counts, OOO_DAYS, "linear")
    result["S2_drop_DDD"] = analyze_late(counts, [DDD_DAY], "linear")
    result["S3_drop_both"] = analyze_late(counts, OOO_DAYS + [DDD_DAY], "linear")
    result["S4_rolling_median"] = analyze_late(counts, [], "rolling")
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

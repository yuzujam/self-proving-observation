# self-proving-observation/
# └── scripts/
#     └── reverify_paper_numbers.py
#
# 論文の数値を、観測終了後にS3互換ストレージへ残った
# 元データ
# （日次Parquet・results/の各CSV/JSON）から再計算するための対話セッション向け
# スクリプト（2026-10-02の全数値の再検証で使った手順の整理。）。
# analyze_periodicity_final.py（6.3）・pool_control_experiment_stats.py（6.1）・
# render_resource_table.py（6.5の表）・analyze_ablation_batch.py（6.6）は別スクリプト。
# 取得は読み取りのみ（rclone copy）。例:
#   rclone copy "<rclone-remote>:<bucket>/clickhouse/heartbeats/" hb
#   python scripts/reverify_paper_numbers.py heartbeat hb
#
# サブコマンド:
#   heartbeat DIR          6.2: 90日窓（129,600分）のカバレッジ・欠損区間・重複・窓後の行
#   fidelity DIR           6.4: results/fidelity/のcron_run*から独立ブロックを数えn=80を再集計
#   ablation DIR [DIR..]   6.6: ablation_batch_*のプール（試行数・一致数・CPUピーク）
#   shap-repeat            7.3: KernelExplainerの再計算間コサイン類似度（要torch・shap）
#   loss-batchlevel DIR    6.1: バッチを単位にした対応ありの厳密な符号反転検定
#                          （DIR=resource CSVのバッチ親DIR）
#   cpu-vs-production      6.5: バッチ別CPUと本番イベント量の相関
#                          （--batches=resource CSVのバッチ親DIR、
#                          --health=pipeline_health ParquetのDIR）

import argparse
import csv
import glob
import json
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

WINDOW_MINUTES = 129_600  # 90日


def cmd_heartbeat(args: argparse.Namespace) -> None:
    frames = [pd.read_parquet(f) for f in sorted(glob.glob(os.path.join(args.dir, "*.parquet")))]
    df = pd.concat(frames)
    df["t"] = pd.to_datetime(df["timestamp"], utc=True)
    print(f"files={len(frames)} rows={len(df)}")
    for node, g in df.groupby("node_id"):
        slot0 = g["t"].min().floor("min")
        slots = pd.date_range(slot0, periods=WINDOW_MINUTES, freq="min")
        minute = g["t"].dt.floor("min")
        inside = g[(minute >= slots[0]) & (minute <= slots[-1])]
        after = g[minute > slots[-1]]
        have = set(minute)
        missing = [s for s in slots if s not in have]
        intervals: list[list[pd.Timestamp]] = []
        for m in missing:
            if intervals and m - intervals[-1][1] == pd.Timedelta(minutes=1):
                intervals[-1][1] = m
            else:
                intervals.append([m, m])
        per_min = minute[(minute >= slots[0]) & (minute <= slots[-1])].value_counts()
        dup_extra = int((per_min[per_min > 1] - 1).sum())
        print(
            f"{node}: window {slots[0]}..{slots[-1]} rows_in_window={len(inside)} "
            f"missing={len(missing)} coverage={100 * (1 - len(missing) / WINDOW_MINUTES):.4f}% "
            f"row_ratio={100 * len(inside) / WINDOW_MINUTES:.4f}% rows_after={len(after)} "
            f"intervals={len(intervals)} duplicate_extra_rows={dup_extra}"
        )
        for a, b in intervals:
            print(f"  gap {a} .. {b} ({int((b - a) / pd.Timedelta(minutes=1)) + 1} min)")


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def cmd_fidelity(args: argparse.Namespace) -> None:
    runs = sorted(glob.glob(os.path.join(args.dir, "cron_run*/")))
    seen: dict[tuple[str, int, int], str] = {}
    pool: dict[str, list[dict[str, object]]] = defaultdict(list)
    for run in runs:
        name = os.path.basename(run.rstrip("/\\"))
        files = glob.glob(os.path.join(run, "*_trial*", "fidelity_results.json"))
        dup = 0
        for f in files:
            d = json.load(open(f, encoding="utf-8"))
            key = (d["scenario"], d["trial"], d["seed"])
            if key in seen:
                dup += 1
                continue
            seen[key] = name
            pool[d["scenario"]].append(d)
        print(f"{name}: trials={len(files)} duplicates_of_earlier_runs={dup}")
    for sc, rs in sorted(pool.items()):
        n = len(rs)
        conf = [r for r in rs if r.get("confirming_detected")]
        lags = [r["detection_lag"] for r in conf]
        lo, hi = _wilson(len(conf), n)
        leads = sum(1 for r in rs if r.get("fidelity_leads"))
        print(
            f"{sc}: n={n} confirming={len(conf)} ({100 * len(conf) / n:.1f}%) "
            f"CI=[{100 * lo:.1f},{100 * hi:.1f}] half_width={50 * (hi - lo):.2f} "
            f"mean_lag={np.mean(lags):.2f} leads={leads}"
        )


def cmd_ablation(args: argparse.Namespace) -> None:
    for batch_dirs in [[d] for d in args.dirs] + ([args.dirs] if len(args.dirs) > 1 else []):
        by_window: dict[int, list[dict[str, float]]] = defaultdict(list)
        for batch in batch_dirs:
            for d in sorted(glob.glob(os.path.join(batch, "w*_trial*"))):
                rp, cp = os.path.join(d, "results.json"), os.path.join(d, "resource_vector.csv")
                if not (os.path.exists(rp) and os.path.exists(cp)):
                    continue
                r = json.load(open(rp, encoding="utf-8"))
                cpu = [float(x["cpu_percent"]) for x in csv.DictReader(open(cp, encoding="utf-8"))]
                by_window[r["window_ms"]].append(
                    {
                        "match": float(r["count_matches"]),
                        "peak": max(cpu),
                        # a trial can lack a latency measurement (None): count it, do not hide it
                        "lower": float("nan")
                        if r["latency_lower_ms"]["mean"] is None
                        else float(r["latency_lower_ms"]["mean"]),
                    }
                )
        label = "+".join(os.path.basename(b) for b in batch_dirs)
        print(f"== {label}")
        for w in sorted(by_window):
            ts = by_window[w]
            print(
                f"window {w} ms: n={len(ts)} matched={int(sum(t['match'] for t in ts))} "
                f"cpu_peak_mean={np.mean([t['peak'] for t in ts]):.3f} "
                f"latency_lower_mean={np.nanmean([t['lower'] for t in ts]):.1f} "
                f"(trials without latency: {sum(1 for t in ts if np.isnan(t['lower']))})"
            )


def cmd_shap_repeat(args: argparse.Namespace) -> None:
    import itertools

    import run_fidelity_experiment as rfe  # type: ignore[import-not-found]
    import torch

    from src.ml.fidelity_guard import FidelityGuard
    from src.ml.lstm_model import train_model
    from src.ml.preprocess import SEQUENCE_LENGTH, create_sequences, normalize

    seed = args.seed
    np.random.seed(seed)
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    normal = rfe.generate_normal_data(200, rng)
    full = rfe.inject_sudden_drift(rfe.generate_normal_data(300, rng), 200, rng)
    norm, mean, std = normalize(normal)
    x, y = create_sequences(norm, SEQUENCE_LENGTH)
    split = int(len(x) * 0.8)
    model = train_model(x[:split], y[:split], epochs=50, batch_size=32)
    xf, _ = create_sequences((full - mean) / std, SEQUENCE_LENGTH)
    wins = [xf[i * 10:(i + 1) * 10] for i in range(len(xf) // 10)]
    fg = FidelityGuard(model, x[:split][:20])

    def cos(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    for wi in (0, 5):
        for ns in (30, 100, 300, 500):
            vals = [fg.compute_shap_values(wins[wi], nsamples=ns) for _ in range(args.repeats)]
            prof = [np.abs(v).reshape(len(v), -1).mean(axis=0) for v in vals]
            pc = [cos(a, b) for a, b in itertools.combinations(prof, 2)]
            print(f"window {wi} nsamples {ns}: profile cosine min {min(pc):.3f} max {max(pc):.3f}")


def cmd_cpu_vs_production(args: argparse.Namespace) -> None:
    from scipy import stats

    from src.measure.aggregate import aggregate_resource_totals

    paths = glob.glob(os.path.join(args.health, "*.parquet"))
    health = pd.concat([pd.read_parquet(f) for f in paths])
    health["t"] = pd.to_datetime(health["checked_at"], utc=True)
    health = health.sort_values("t").set_index("t")
    rows = []
    for b in sorted(os.listdir(args.batches)):
        m = re.match(r"batch_(\d{8})_(\d{6})", b)
        pattern = os.path.join(args.batches, b, "**", "resource_proposed.csv")
        files = glob.glob(pattern, recursive=True)
        if not m or not files:
            continue
        t0 = pd.Timestamp(m.group(1) + m.group(2), tz="Europe/Berlin").tz_convert("UTC")
        lo, hi = t0 - pd.Timedelta(hours=1), t0 + pd.Timedelta(hours=4)
        win = health[(health.index >= lo) & (health.index <= hi)]
        if win.empty:
            continue
        agg = aggregate_resource_totals(files, "proposed-")
        rows.append((b, agg["cpu_percent_total"]["mean"], float(win["events_last_hour"].mean())))
    df = pd.DataFrame(rows, columns=["batch", "cpu_mean", "events_per_hour"])
    # 合成注入が本番テーブルに混入した週（07-07〜07-13）は本番イベント量として使えない
    df = df[~df["batch"].str.contains("20260712|20260713")]
    rho = stats.spearmanr(df["cpu_mean"], df["events_per_hour"])
    print(df.to_string(index=False))
    print(f"batches={len(df)} spearman rho={rho[0]:.2f} p={rho[1]:.3f}")
    rest = df[~df["batch"].str.contains("20260724")]
    rho2 = stats.spearmanr(rest["cpu_mean"], rest["events_per_hour"])
    print(f"without 07-24: batches={len(rest)} spearman rho={rho2[0]:.2f} p={rho2[1]:.3f}")


def _signflip_exact(values: list[float]) -> tuple[float, int, int]:
    """平均差に対する厳密な両側符号反転検定（0は情報を持たない）。"""
    import itertools

    nz = np.array([v for v in values if v != 0.0])
    n = len(nz)
    if n == 0:
        return 1.0, 0, 0
    obs = abs(nz.sum())
    hits = 0
    for signs in itertools.product((-1.0, 1.0), repeat=n):
        if abs(float((np.array(signs) * np.abs(nz)).sum())) >= obs - 1e-12:
            hits += 1
    return hits / 2**n, int((nz > 0).sum()), int((nz < 0).sum())


def cmd_loss_batchlevel(args: argparse.Namespace) -> None:
    diffs: dict[tuple[str, int], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for b in sorted(os.listdir(args.dir)):
        if "20260713" in b:  # 試行が18件しかない部分バッチ（6.1の17バッチに含めない）
            continue
        for d in glob.glob(os.path.join(args.dir, b, "*_rps*_trial*")):
            m = re.match(r"(\w+?)_rps(\d+)_trial(\d+)", os.path.basename(d))
            f = os.path.join(d, "loss_rate.json")
            if not m or not os.path.exists(f):
                continue
            j = json.load(open(f, encoding="utf-8"))
            vals: list[float | None] = []
            for side in ("baseline", "proposed"):
                x = j.get(side)
                ok = x and not x.get("verification_failed") and "loss_rate_percent" in x
                vals.append(x["loss_rate_percent"] if ok else None)
            if vals[0] is not None and vals[1] is not None:
                diffs[(m.group(1), int(m.group(2)))][b].append(vals[0] - vals[1])
    out = []
    for key in sorted(diffs):
        bm = [float(np.mean(v)) for v in diffs[key].values()]
        p, pos, neg = _signflip_exact(bm)
        out.append((key, len(bm), pos, neg, p))
    m_tests = len(out)
    for key, n, pos, neg, p in out:
        print(
            f"{key}: batches={n} positive={pos} negative={neg} "
            f"exact_p={p:.4g} bonferroni={min(1.0, p * m_tests):.3g}"
        )
    print("significant after Bonferroni:", [o[0] for o in out if o[4] * m_tests < 0.05])
    print("significant before correction:", [o[0] for o in out if o[4] < 0.05])


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("heartbeat")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_heartbeat)
    p = sub.add_parser("fidelity")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_fidelity)
    p = sub.add_parser("ablation")
    p.add_argument("dirs", nargs="+")
    p.set_defaults(fn=cmd_ablation)
    p = sub.add_parser("shap-repeat")
    p.add_argument("--seed", type=int, default=43)
    p.add_argument("--repeats", type=int, default=4)
    p.set_defaults(fn=cmd_shap_repeat)
    p = sub.add_parser("loss-batchlevel")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_loss_batchlevel)
    p = sub.add_parser("cpu-vs-production")
    p.add_argument("--batches", required=True)
    p.add_argument("--health", required=True)
    p.set_defaults(fn=cmd_cpu_vs_production)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

# self-proving-observation/
# └── scripts/
#     └── run_fidelity_experiment.py  — Fidelity Guard 実験の自動化

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import torch

# `python3 scripts/run_fidelity_experiment.py` のように直接パス実行された場合、
# プロジェクトルートが sys.path に乗らず `from src...` の import が失敗する。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_config import ensure_utf8_stdio
from src.ml.fidelity_guard import FidelityGuard
from src.ml.lstm_model import evaluate, predict, save_model, train_model
from src.ml.preprocess import SEQUENCE_LENGTH, create_sequences, normalize
from src.visualize.report import plot_fidelity_timeline

ensure_utf8_stdio()

MAX_CONFIRMING_LAG = 8  # 確認指標としての許容後追い幅（実測レンジ 1〜8 ウィンドウ）

FEATURE_NAMES = [
    "event_count", "unique_sensors", "avg_severity", "max_severity",
    "alert_count", "dns_count", "http_count", "tls_count",
    "flow_count", "ssh_count",
]


def generate_normal_data(n_windows: int, rng: np.random.Generator) -> np.ndarray:
    """定常状態の攻撃トラフィックを模した合成データを生成する。"""
    data = np.zeros((n_windows, len(FEATURE_NAMES)), dtype=np.float32)

    data[:, 0] = rng.poisson(50, n_windows)
    data[:, 1] = rng.poisson(5, n_windows)
    data[:, 2] = rng.normal(2.5, 0.3, n_windows).clip(1, 4)
    data[:, 3] = rng.choice([3, 4], n_windows, p=[0.7, 0.3])
    data[:, 4] = rng.poisson(5, n_windows)
    data[:, 5] = rng.poisson(15, n_windows)
    data[:, 6] = rng.poisson(10, n_windows)
    data[:, 7] = rng.poisson(8, n_windows)
    data[:, 8] = rng.poisson(7, n_windows)
    data[:, 9] = rng.poisson(5, n_windows)

    return data


def inject_sudden_drift(data: np.ndarray, drift_start: int, rng: np.random.Generator) -> np.ndarray:
    """突発的な概念ドリフトを注入する。"""
    drifted = data.copy()
    n = len(drifted) - drift_start

    drifted[drift_start:, 0] = rng.poisson(200, n)
    drifted[drift_start:, 4] = rng.poisson(50, n)
    drifted[drift_start:, 2] = rng.normal(3.8, 0.2, n).clip(1, 4)
    drifted[drift_start:, 3] = 4
    drifted[drift_start:, 9] = rng.poisson(40, n)

    return drifted


def inject_gradual_drift(
    data: np.ndarray, drift_start: int, rng: np.random.Generator,
) -> np.ndarray:
    """緩やかな概念ドリフトを注入する。"""
    drifted = data.copy()
    drift_len = len(drifted) - drift_start

    for i in range(drift_len):
        progress = i / max(drift_len - 1, 1)
        idx = drift_start + i
        drifted[idx, 0] += progress * 150 + rng.normal(0, 10)
        drifted[idx, 4] += progress * 45 + rng.normal(0, 5)
        drifted[idx, 2] += progress * 1.3
        drifted[idx, 9] += progress * 35 + rng.normal(0, 3)

    drifted = drifted.clip(0, None)
    drifted[:, 2] = drifted[:, 2].clip(1, 4)
    return drifted


def inject_recurring_drift(
    data: np.ndarray, drift_start: int, rng: np.random.Generator,
) -> np.ndarray:
    """周期的な概念ドリフトを注入する。"""
    drifted = data.copy()
    drift_len = len(drifted) - drift_start
    period = max(drift_len // 3, 1)

    for i in range(drift_len):
        idx = drift_start + i
        phase = (i % period) / period
        if phase < 0.5:
            intensity = phase * 2
        else:
            intensity = (1 - phase) * 2

        drifted[idx, 0] += intensity * 150 + rng.normal(0, 5)
        drifted[idx, 4] += intensity * 40 + rng.normal(0, 3)
        drifted[idx, 9] += intensity * 30 + rng.normal(0, 3)

    drifted = drifted.clip(0, None)
    return drifted


DRIFT_SCENARIOS = {
    "sudden": inject_sudden_drift,
    "gradual": inject_gradual_drift,
    "recurring": inject_recurring_drift,
}


def inject_no_drift(data: np.ndarray, drift_start: int, rng: np.random.Generator) -> np.ndarray:
    """ドリフトを注入しない対照（null）条件。検知器が正常データに誤警報を出す割合の測定用。

    doc/known-limitations.md #FFF: 既存の3シナリオは全てドリフトを含み、確認検知率は
    「ドリフトがあるときに検知する割合（感度）」に限られていた。ベースライン確立後も
    ドリフトの無い窓を続けたデータに同じ検知器・同じ判定を適用して、誤警報を数える。
    """
    return data.copy()


# DRIFT_SCENARIOS には入れない: `--scenario all`・fidelity_summary.json（貢献3の確定結果）の
# 出力を変えないため。`--scenario none` で明示した時だけ実行する。
NULL_SCENARIO_NAME = "none"
NULL_SCENARIOS = {NULL_SCENARIO_NAME: inject_no_drift}


def false_alarm_windows(fidelity_results: list[dict], first_eval_window: int) -> list[int]:
    """検知が有効になる窓（first_eval_window以降）のうち drift_detected が True の窓番号。"""
    return [
        r["window_index"]
        for r in fidelity_results
        if r["window_index"] >= first_eval_window and r["drift_detected"]
    ]


def run_single_scenario(
    scenario_name: str,
    drift_fn,
    output_dir: str,
    n_normal: int = 200,
    n_total: int = 300,
    seq_len: int = SEQUENCE_LENGTH,
    epochs: int = 50,
    trial: int = 1,
    seed: int = 42,
) -> dict:
    """1シナリオの Fidelity Guard 実験を実行する。"""
    # rng（データ生成用）はシード固定済みだが、LSTM の重み初期化・
    # DataLoader の shuffle・SHAP KernelExplainer のサンプリングは
    # torch / numpy のグローバル乱数状態に依存しており未固定だった。
    # そのため同一コードの再実行でも試行ごとに別モデル・別ノイズが
    # 生成され、fidelity_leads の結果が再現しなかった（実測で確認済み：
    # 同一スクリプトの2回の実行で sudden と gradual の成否が入れ替わった）。
    trial_seed = seed + trial
    np.random.seed(trial_seed)
    torch.manual_seed(trial_seed)
    rng = np.random.default_rng(trial_seed)

    print(f"\n{'='*50}")
    print(f" Scenario: {scenario_name} (trial {trial})")
    print(f"{'='*50}")

    normal_data = generate_normal_data(n_normal, rng)
    full_data = generate_normal_data(n_total, rng)
    full_data = drift_fn(full_data, n_normal, rng)

    norm_data, mean, std = normalize(normal_data)
    X_train, y_train = create_sequences(norm_data, seq_len)

    split = int(len(X_train) * 0.8)
    X_tr, y_tr = X_train[:split], y_train[:split]
    X_val, y_val = X_train[split:], y_train[split:]

    print(f"[FG] Training LSTM (epochs={epochs})...")
    model = train_model(X_tr, y_tr, epochs=epochs, batch_size=32)

    val_metrics = evaluate(model, X_val, y_val)
    print(f"[FG] Validation — MSE: {val_metrics['mse']:.6f}  MAE: {val_metrics['mae']:.6f}")

    full_norm = (full_data - mean) / std
    X_full, y_full = create_sequences(full_norm, seq_len)

    window_size = 10
    n_windows = len(X_full) // window_size
    X_windows = [X_full[i * window_size:(i + 1) * window_size] for i in range(n_windows)]

    print(f"[FG] Running Fidelity Guard ({n_windows} windows)...")

    drift_window_approx = (n_normal - seq_len) // window_size

    background = X_tr[:20]
    fg = FidelityGuard(model, background)
    # nsamples=30 は KernelExplainer への入力（seq_len×n_features=120次元）に対して
    # 少なすぎ、同一ウィンドウ・同一モデルへの2回のSHAP計算ですら再現性がなかった
    # （repeat-to-repeat cosine similarity が nsamples=30 で 0.01〜0.43、
    # nsamples=500 で 0.97 以上に収束することを実測で確認）。300 で妥当な収束と
    # 実行時間のバランスを取る。
    fidelity_results = fg.monitor_drift(
        X_windows, window_threshold=0.5, nsamples=300,
        threshold_mode="adaptive", baseline_windows=max(drift_window_approx, 1),
    )

    accuracy_per_window = []
    for i in range(n_windows):
        w_X = X_full[i * window_size:(i + 1) * window_size]
        w_y = y_full[i * window_size:(i + 1) * window_size]
        preds = predict(model, w_X)
        mse = float(np.mean((preds - w_y) ** 2))
        accuracy_per_window.append({"window_index": i, "mse": round(mse, 6)})

    scenario_result = {
        "scenario": scenario_name,
        "trial": trial,
        "seed": seed + trial,
        "n_normal_windows": n_normal,
        "n_total_windows": n_total,
        "drift_start_window": drift_window_approx,
        "validation_metrics": val_metrics,
        "fidelity_results": fidelity_results,
        "accuracy_per_window": accuracy_per_window,
    }

    first_fidelity_drop = None
    for r in fidelity_results:
        if r["drift_detected"]:
            first_fidelity_drop = r["window_index"]
            break

    first_accuracy_drop = None
    if accuracy_per_window and drift_window_approx > 0:
        baseline_mse = np.mean([a["mse"] for a in accuracy_per_window[:drift_window_approx]])
        threshold = baseline_mse * 3 if not np.isnan(baseline_mse) else float("inf")
        for a in accuracy_per_window:
            if a["window_index"] >= drift_window_approx and a["mse"] > threshold:
                first_accuracy_drop = a["window_index"]
                break

    scenario_result["first_fidelity_drop_window"] = first_fidelity_drop
    scenario_result["first_accuracy_drop_window"] = first_accuracy_drop
    # 「先行指標」の証明には厳密な先行（<）が必要。同一ウィンドウは「先行」に含めない。
    # 実験（乱数シード固定・十分な SHAP サンプル数）で shap_drift_score は
    # 先行指標ではなく確認指標（1〜8ウィンドウ後追い）と判明したため
    # （src/ml/fidelity_guard.py FidelityGuard docstring 参照）、
    # fidelity_leads は構造的にほぼ常に False になる。この値自体は
    # 「先行していないこと」の記録として引き続き残し、実際の成功判定には
    # detection_lag / confirming_detected を用いる。
    scenario_result["fidelity_leads"] = (
        first_fidelity_drop is not None
        and first_accuracy_drop is not None
        and first_fidelity_drop < first_accuracy_drop
    )

    detection_lag = None
    if first_fidelity_drop is not None and first_accuracy_drop is not None:
        detection_lag = first_fidelity_drop - first_accuracy_drop
    scenario_result["detection_lag"] = detection_lag
    # 確認指標としての成功基準: 精度低下と同時、またはそれ以降
    # MAX_CONFIRMING_LAG ウィンドウ以内に検知できたか
    # （実測レンジ 1〜8 ウィンドウに基づく。detection_lag が負＝先行検知も含む）。
    scenario_result["confirming_detected"] = (
        detection_lag is not None and detection_lag <= MAX_CONFIRMING_LAG
    )

    if scenario_name == NULL_SCENARIO_NAME:
        # nullではドリフトが無いため上の検知系の値は意味を持たない。代わりに、検知が有効に
        # なる窓（ベースライン確立窓 baseline_windows-1 以降）の誤警報を記録する。
        first_eval_window = max(drift_window_approx, 1) - 1
        scenario_result["false_alarm_windows"] = false_alarm_windows(
            fidelity_results, first_eval_window,
        )
        scenario_result["n_evaluated_windows"] = n_windows - first_eval_window

    scenario_dir = os.path.join(output_dir, f"{scenario_name}_trial{trial}")
    os.makedirs(scenario_dir, exist_ok=True)

    with open(os.path.join(scenario_dir, "fidelity_results.json"), "w") as f:
        json.dump(scenario_result, f, indent=2, default=str)

    save_model(model, os.path.join(scenario_dir, "model.pt"))

    plot_fidelity_timeline(fidelity_results, os.path.join(scenario_dir, "fidelity_timeline.png"))

    plot_fidelity_vs_accuracy(
        fidelity_results, accuracy_per_window, drift_window_approx,
        os.path.join(scenario_dir, "fidelity_vs_accuracy.png"),
    )

    return scenario_result


def plot_fidelity_vs_accuracy(
    fidelity_results: list[dict],
    accuracy_results: list[dict],
    drift_start: int,
    output_path: str,
):
    """Fidelity スコア vs 予測精度の比較プロットを生成する。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

    windows_f = [r["window_index"] for r in fidelity_results]
    scores = [r["fidelity_score"] for r in fidelity_results]
    moving_avgs = [r["moving_avg"] for r in fidelity_results]

    ax1.plot(
        windows_f, scores, "o-", markersize=3,
        label="Fidelity Score", color="#3498db", alpha=0.6,
    )
    ax1.plot(windows_f, moving_avgs, "-", linewidth=2, label="Moving Avg (5)", color="#e67e22")
    ax1.axhline(y=0.5, color="#95a5a6", linestyle=":", alpha=0.5, label="Threshold")
    ax1.axvline(x=drift_start, color="#e74c3c", linestyle="--", alpha=0.7, label="Drift Injected")
    ax1.set_ylabel("Fidelity Score")
    ax1.set_title("Fidelity Guard — Concept Drift Detection")
    ax1.set_ylim(-0.05, 1.05)
    ax1.legend(fontsize=8)
    ax1.grid(True, alpha=0.3)

    windows_a = [r["window_index"] for r in accuracy_results]
    mses = [r["mse"] for r in accuracy_results]

    ax2.plot(
        windows_a, mses, "o-", markersize=3,
        label="Prediction MSE", color="#9b59b6", alpha=0.8,
    )
    ax2.axvline(x=drift_start, color="#e74c3c", linestyle="--", alpha=0.7, label="Drift Injected")
    ax2.set_ylabel("Prediction MSE")
    ax2.set_xlabel("Window Index")
    ax2.set_title("Prediction Accuracy over Time")
    ax2.legend(fontsize=8)
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    plt.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close()
    print(f"[VIZ] Fidelity vs Accuracy plot saved to {output_path}")


def summarize_scenario_results(scenario_runs: list[dict]) -> dict:
    """1シナリオ分の trial 結果リストから fidelity_summary.json 用の集計を作る。

    mean_detection_lag は confirming_detected な trial（detection_lag が
    MAX_CONFIRMING_LAG 以内）に限定する。「検知trialのみ」という文書上の
    定義と一致させるための絞り込みで、detection_lag はあるが
    MAX_CONFIRMING_LAG を超えて確認指標として扱われない trial（実測レン
    ジ内では未発火だが理論上は起こりうる）を平均から除外する。
    """
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


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二項割合のWilson score区間（既存の確認検知率の区間と同じ方法、95%）。"""
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize_null_results(null_runs: list[dict]) -> dict:
    """null条件（ドリフト無し）の trial 結果から誤警報の集計を作る。

    試行は独立なシード（seed+trial）なので、試行単位の割合（1窓でも誤警報が出た試行の割合）に
    Wilson区間を付ける。窓単位の割合は同一試行内で相関するため記述統計のみとし、区間は付けない。
    """
    n_trials = len(null_runs)
    trials_with_alarm = sum(1 for r in null_runs if r["false_alarm_windows"])
    alarm_windows = sum(len(r["false_alarm_windows"]) for r in null_runs)
    evaluated_windows = sum(r["n_evaluated_windows"] for r in null_runs)
    lo, hi = _wilson_interval(trials_with_alarm, n_trials)
    return {
        "n_trials": n_trials,
        "false_alarm_trial_count": trials_with_alarm,
        "false_alarm_trial_rate": round(trials_with_alarm / max(n_trials, 1), 4),
        "false_alarm_trial_rate_wilson95": [round(lo, 4), round(hi, 4)],
        "false_alarm_window_count": alarm_windows,
        "evaluated_window_count": evaluated_windows,
        "false_alarm_window_rate": round(alarm_windows / max(evaluated_windows, 1), 4),
    }


def run_all_scenarios(
    output_dir: str,
    n_trials: int = 3,
    epochs: int = 50,
    seed: int = 42,
):
    """全ドリフトシナリオ × 複数試行の実験を実行する。"""
    all_results = []

    for scenario_name, drift_fn in DRIFT_SCENARIOS.items():
        for trial in range(1, n_trials + 1):
            result = run_single_scenario(
                scenario_name=scenario_name,
                drift_fn=drift_fn,
                output_dir=output_dir,
                trial=trial,
                epochs=epochs,
                seed=seed,
            )
            all_results.append(result)

    summary = {
        "total_experiments": len(all_results),
        "scenarios": {
            scenario_name: summarize_scenario_results(
                [r for r in all_results if r["scenario"] == scenario_name]
            )
            for scenario_name in DRIFT_SCENARIOS
        },
    }

    summary_path = os.path.join(output_dir, "fidelity_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n[FG] Summary saved to {summary_path}")

    print("\n=== Fidelity Guard Experiment Summary ===")
    for name, stats in summary["scenarios"].items():
        lag_str = (
            f"{stats['mean_detection_lag']} windows"
            if stats["mean_detection_lag"] is not None
            else "n/a"
        )
        print(
            f"  {name}: confirming detection in "
            f"{stats['confirming_detected_count']}/{stats['n_trials']} trials "
            f"({stats['confirming_detected_rate']*100:.0f}%, mean lag={lag_str}) | "
            f"leads accuracy in {stats['fidelity_leads_count']}/{stats['n_trials']} "
            f"({stats['fidelity_leads_rate']*100:.0f}%)"
        )

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Fidelity Guard 実験自動化",
    )
    parser.add_argument(
        "--output-dir", default="results/fidelity",
        help="結果出力ディレクトリ",
    )
    parser.add_argument(
        "--trials", type=int, default=3,
        help="各シナリオの試行回数",
    )
    parser.add_argument(
        "--epochs", type=int, default=50,
        help="LSTM 学習エポック数",
    )
    parser.add_argument(
        "--scenario",
        choices=list(DRIFT_SCENARIOS.keys()) + [NULL_SCENARIO_NAME, "all"], default="all",
        help="実行するドリフトシナリオ。none はドリフトを注入しない対照条件（誤警報の測定、"
             "#FFF）で、--scenario all には含まれない。確定結果のディレクトリ（results/fidelity）"
             "とは別の --output-dir を指定すること",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="trial_seed = seed + trial の基準値。cron等で複数回実行する際に"
             "run間で重ならない値を渡すことで、独立した新規試行として積み増せる"
             "（既定値のままだと毎回同じ乱数列＝同じ結果を再生するだけになる）",
    )

    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    if args.scenario == "all":
        run_all_scenarios(args.output_dir, args.trials, args.epochs, args.seed)
    else:
        scenario_fns = {**DRIFT_SCENARIOS, **NULL_SCENARIOS}
        runs = []
        for trial in range(1, args.trials + 1):
            runs.append(run_single_scenario(
                scenario_name=args.scenario,
                drift_fn=scenario_fns[args.scenario],
                output_dir=args.output_dir,
                trial=trial,
                epochs=args.epochs,
                seed=args.seed,
            ))
        if args.scenario == NULL_SCENARIO_NAME:
            null_summary = summarize_null_results(runs)
            summary_path = os.path.join(args.output_dir, "fidelity_null_summary.json")
            with open(summary_path, "w") as f:
                json.dump(null_summary, f, indent=2)
            print(f"\n[FG] Null-condition summary saved to {summary_path}")
            print(
                f"  none: false alarm in {null_summary['false_alarm_trial_count']}/"
                f"{null_summary['n_trials']} trials "
                f"(Wilson95 {null_summary['false_alarm_trial_rate_wilson95']}), "
                f"{null_summary['false_alarm_window_count']}/"
                f"{null_summary['evaluated_window_count']} evaluated windows"
            )


if __name__ == "__main__":
    main()

# self-proving-observation/
# └── src/
#     └── ml/
#         └── fidelity_guard.py  — Fidelity Guard: SHAP + 概念ドリフト検知

from typing import Any

import numpy as np
import shap

from src.logging_config import get_logger
from src.ml.lstm_model import ThreatLSTM, predict

logger = get_logger(__name__)


class FidelityGuard:
    """XAI（SHAP）を用いた概念ドリフト検知。

    Fidelity スコア（compute_fidelity_score）= SHAP 加法的説明が
    モデル自身の現在の出力をどれだけ再現できるかの R² 指標。
    導入時は「精度低下より先に Fidelity が崩壊する先行指標」を
    想定していたが、再現性を確保した実験（乱数シード固定・
    十分な nsamples）で 9/9 試行すべて 1.0000 に張り付いたままと
    判明し、モデル自身の出力を再現するという性質上、概念ドリフトに
    構造的に無反応であることを確認した。

    実際にドリフトを捉えるのは shap_drift_score（SHAP 説明の
    特徴量寄与プロファイルが正常期間からどれだけ乖離したか）で、
    9/9 試行で正常期間の統計から有意に低下する。ただし精度低下
    （baseline_mse の3倍超）は生の単一ウィンドウ値で即座に反応する
    ため、shap_drift_score による検知はそれより 1〜8 ウィンドウ
    後追いになる（＝先行指標ではなく確認指標）。fidelity_score は
    無反応であることの記録として引き続き算出・返却する。
    """

    def __init__(
        self,
        model: ThreatLSTM,
        background_data: np.ndarray,
        device: str = "cpu",
    ):
        self.model = model
        self.device = device
        self.background = background_data
        self._seq_shape = background_data.shape[1:]  # (seq_len, n_features)
        background_2d = background_data.reshape(len(background_data), -1)

        # model_fn はスカラー出力（出力次元の平均）として SHAP に渡す。
        # 多出力のまま渡すと SHAP が出力ごとに値を分割し、
        # 平均後に加法性が崩れて fidelity が常に 0 になる。
        def model_fn(x_2d: np.ndarray) -> np.ndarray:
            x_3d = x_2d.reshape(len(x_2d), *self._seq_shape)
            return predict(model, x_3d, device).mean(axis=1)  # (n,)

        self.explainer = shap.KernelExplainer(model_fn, background_2d)

    def compute_shap_values(self, X: np.ndarray, nsamples: int = 100) -> np.ndarray:
        """SHAP 値を算出する。

        model_fn がスカラー出力のため、KernelExplainer は
        (n_samples, n_features_2d) の2次元配列を返す。
        """
        X_2d = X.reshape(len(X), -1)
        raw = self.explainer.shap_values(X_2d, nsamples=nsamples)
        shap_2d = np.asarray(raw)
        # 一部の SHAP バージョンは (1, n_samples, n_features) を返す場合がある
        if shap_2d.ndim == 3:
            shap_2d = shap_2d.squeeze(0)
        return shap_2d.reshape(X.shape)

    def compute_fidelity_score(
        self,
        X: np.ndarray,
        shap_values: np.ndarray,
    ) -> float:
        """Fidelity スコアを算出する。

        SHAP の加法的説明（base_value + sum(shap_values)）が
        元モデルの予測をどれだけ再現するかを R² で評価する。
        model_fn の出力（出力次元の平均）と比較する。
        """
        actual = predict(self.model, X, self.device)  # (batch, output_dim)
        actual_scalar = actual.mean(axis=1)  # (batch,) — model_fn と同一の集約

        base_value = self.explainer.expected_value
        if isinstance(base_value, (list, np.ndarray)):
            base_value = float(np.mean(base_value))

        shap_contribution = shap_values.reshape(len(X), -1).sum(axis=1)  # (batch,)
        explained = base_value + shap_contribution  # (batch,)

        ss_res = np.sum((actual_scalar - explained) ** 2)
        ss_tot = np.sum((actual_scalar - actual_scalar.mean()) ** 2)

        if ss_tot == 0:
            return 1.0 if ss_res == 0 else 0.0

        r2 = 1.0 - (ss_res / ss_tot)
        return float(np.clip(r2, 0.0, 1.0))

    def compute_shap_drift_score(
        self,
        shap_values: np.ndarray,
        baseline_shap_values: np.ndarray,
    ) -> float:
        """SHAP 値の特徴量寄与プロファイルが正常期間（baseline）からどれだけ
        乖離したかをコサイン類似度で測る（1.0=同一、0.0=無相関）。

        compute_fidelity_score は「モデル自身の現在の出力」を再現できるかを
        見る指標のため、モデルの重みが変化しない限り SHAP の加法再構成は
        常に高くなりがちで、概念ドリフトに対して原理的に鈍感（本研究での
        検証で sudden/gradual/recurring 全シナリオにおいて fidelity が
        ほぼ 1.0 に張り付き、ドリフト注入と無相関な単発ノイズしか出なかった
        ことで確認済み）。こちらは「どの特徴量が予測に効いているか」という
        説明の構造そのものの変化を捉える、独立した補助シグナル。
        """
        profile = np.abs(shap_values).reshape(len(shap_values), -1).mean(axis=0)
        baseline_profile = np.abs(baseline_shap_values).reshape(
            len(baseline_shap_values), -1,
        ).mean(axis=0)

        norm_p = np.linalg.norm(profile)
        norm_b = np.linalg.norm(baseline_profile)
        if norm_p == 0 or norm_b == 0:
            return 1.0 if norm_p == norm_b else 0.0

        cos_sim = float(np.dot(profile, baseline_profile) / (norm_p * norm_b))
        return float(np.clip(cos_sim, 0.0, 1.0))

    def monitor_drift(
        self,
        X_windows: list[np.ndarray],
        window_threshold: float = 0.5,
        nsamples: int = 50,
        threshold_mode: str = "absolute",
        baseline_windows: int = 5,
        drift_zscore: float = 2.0,
        min_margin: float = 0.01,
    ) -> list[dict[str, Any]]:
        """時系列ウィンドウごとの Fidelity スコアを追跡し、ドリフトを検知する。

        threshold_mode="absolute"（デフォルト）は既存動作と完全互換
        （fidelity_score の移動平均を window_threshold と比較）。

        threshold_mode="adaptive" は drift_detected の判定を
        shap_drift_score（正常期間からの SHAP 説明プロファイルの乖離）
        に切り替える。fidelity_score（SHAP 加法再構成 R²）は実験で
        検証した結果、9/9 試行すべてでドリフト前後を通じて 1.0000 に
        張り付いたままで、モデル自身の出力を再現する指標という性質上
        概念ドリフトに構造的に無反応だった（nsamples を増やして
        ノイズを排除しても変化なし）。一方 shap_drift_score は
        nsamples 不足によるノイズさえ解消すれば（下記 nsamples 参照）
        9/9 試行で正常期間の平均から統計的に有意に低下しており、
        実際にドリフトを捉えていることを確認済み。そのため adaptive
        モードでは shap_drift_score を主指標として使う。fidelity_score
        自身は無反応であることの記録として引き続き返す。

        なお shap_drift_score は KernelExplainer の近似精度に強く依存する。
        seq_len×n_features を平坦化した入力次元に対して nsamples が
        少なすぎると、同一ウィンドウ・同一モデルに対する2回のSHAP計算
        ですら整合しない（120次元の入力で nsamples=30 のとき
        repeat-to-repeat のコサイン類似度が 0.01〜0.43、nsamples=500 で
        0.97 以上に収束することを実測で確認）。呼び出し側で入力次元に
        見合った nsamples を渡すこと。

        正常期間の統計が完全に一定（std≈0）だと z-score のみでは
        閾値が baseline_mean にほぼ密着し、丸め表示には出ない浮動小数点
        誤差レベルの揺らぎにまで反応してしまう。min_margin を
        baseline_mean からの最低乖離幅として下限に噛ませることで、
        意味のある低下だけを検知対象にする。
        """
        results: list[dict[str, Any]] = []
        scores = []
        shap_value_history = []
        baseline_mean: float | None = None
        baseline_std: float = 0.0
        baseline_shap_scores: list[float] = []
        shap_baseline_mean: float | None = None
        shap_baseline_std: float = 0.0

        for i, X_window in enumerate(X_windows):
            shap_vals = self.compute_shap_values(X_window, nsamples=nsamples)
            fidelity = self.compute_fidelity_score(X_window, shap_vals)
            scores.append(fidelity)
            shap_value_history.append(shap_vals)

            moving_avg = np.mean(scores[-5:]) if len(scores) >= 5 else np.mean(scores)
            effective_threshold = window_threshold
            shap_drift_score = None

            if threshold_mode == "adaptive":
                if i == baseline_windows - 1:
                    baseline_mean = float(np.mean(scores[:baseline_windows]))
                    baseline_std = float(np.std(scores[:baseline_windows]))
                    # 正常期間自体の shap_drift_score のブレ幅を把握するため、
                    # 各ベースラインウィンドウについて「自分以外の正常期間
                    # ウィンドウ」との類似度をリーブワンアウトで遡って埋める
                    # （自分自身を含めた比較は自明に高い類似度が出るため使えない）。
                    # j == baseline_windows - 1（今回のウィンドウ自身）は
                    # まだ results に追加されていないため対象外（このウィンドウの
                    # shap_drift_score は下の通常経路でベースライン全体との
                    # 比較として算出される）。
                    for j in range(baseline_windows - 1):
                        other_shap = np.concatenate(
                            shap_value_history[:j] + shap_value_history[j + 1:baseline_windows],
                            axis=0,
                        )
                        score = self.compute_shap_drift_score(
                            shap_value_history[j], other_shap,
                        )
                        results[j]["shap_drift_score"] = round(score, 4)
                        baseline_shap_scores.append(score)
                    shap_baseline_mean = float(np.mean(baseline_shap_scores))
                    shap_baseline_std = float(np.std(baseline_shap_scores))
                if baseline_mean is not None:
                    margin = max(drift_zscore * baseline_std, min_margin)
                    effective_threshold = baseline_mean - margin
                    baseline_shap = np.concatenate(
                        shap_value_history[:baseline_windows], axis=0,
                    )
                    shap_drift_score = self.compute_shap_drift_score(
                        shap_vals, baseline_shap,
                    )

            if threshold_mode == "adaptive" and shap_baseline_mean is not None:
                shap_margin = max(drift_zscore * shap_baseline_std, min_margin)
                shap_effective_threshold = shap_baseline_mean - shap_margin
                drift_detected: bool = (
                    shap_drift_score is not None and shap_drift_score < shap_effective_threshold
                )
            else:
                shap_effective_threshold = None
                # numpy.float64同士の比較はnumpy.bool_を返し、json.dump(default=str)で
                # 文字列"False"（再読込すると真）になる。Pythonのboolへ揃える。
                # 判定ロジック自体は無変更。
                drift_detected = bool(moving_avg < effective_threshold)

            result: dict[str, Any] = {
                "window_index": i,
                "fidelity_score": round(fidelity, 4),
                "moving_avg": round(float(moving_avg), 4),
                "drift_detected": drift_detected,
            }
            if threshold_mode == "adaptive":
                result["effective_threshold"] = (
                    round(effective_threshold, 4) if baseline_mean is not None else None
                )
                result["shap_drift_score"] = (
                    round(shap_drift_score, 4) if shap_drift_score is not None else None
                )
                result["shap_effective_threshold"] = (
                    round(shap_effective_threshold, 4)
                    if shap_effective_threshold is not None
                    else None
                )
            results.append(result)

            status = "DRIFT" if drift_detected else "OK"
            logger.info(
                f"[FG] Window {i:>3d}  "
                f"fidelity={fidelity:.4f}  "
                f"ma5={moving_avg:.4f}  "
                f"[{status}]"
            )

        return results

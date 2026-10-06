# self-proving-observation/
# └── src/
#     └── ml/
#         └── lstm_model.py  — LSTM 時系列予測モデル（PyTorch）

import os

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from src.logging_config import get_logger

logger = get_logger(__name__)


class ThreatLSTM(nn.Module):
    """脅威イベント時系列予測用 LSTM モデル。"""

    def __init__(self, input_dim: int, hidden_dim: int = 64, num_layers: int = 2):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            dropout=0.2,
        )
        self.fc = nn.Linear(hidden_dim, input_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        lstm_out, _ = self.lstm(x)
        out: torch.Tensor = self.fc(lstm_out[:, -1, :])
        return out


def train_model(
    X_train: np.ndarray,
    y_train: np.ndarray,
    epochs: int = 50,
    batch_size: int = 32,
    lr: float = 1e-3,
    device: str = "cpu",
) -> ThreatLSTM:
    """LSTM モデルを学習する。"""
    # torch.manual_seed だけでは CPU 上のマルチスレッド演算（reduction順序）
    # まで決定論的にならず、同一シードでも実行のたびに浮動小数点レベルで
    # 結果が揺れうる（gradual/recurring trial8の
    # detection_lag符号が同一seedにもかかわらず07-06版と07-12版で入れ替わった件の
    # 原因候補）。use_deterministic_algorithms で決定論的な実装を強制する
    # （非対応opは例外を投げずwarn_onlyで警告に留める）。
    torch.use_deterministic_algorithms(True, warn_only=True)
    input_dim = X_train.shape[2]
    model = ThreatLSTM(input_dim).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = nn.MSELoss()

    dataset = TensorDataset(
        torch.tensor(X_train, dtype=torch.float32),
        torch.tensor(y_train, dtype=torch.float32),
    )
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        for X_batch, y_batch in loader:
            X_batch, y_batch = X_batch.to(device), y_batch.to(device)
            optimizer.zero_grad()
            pred = model(X_batch)
            loss = criterion(pred, y_batch)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        avg_loss = total_loss / len(loader)
        if (epoch + 1) % 10 == 0 or epoch == 0:
            logger.info(f"[LSTM] Epoch {epoch + 1}/{epochs}  Loss: {avg_loss:.6f}")

    return model


def predict(
    model: ThreatLSTM,
    X: np.ndarray,
    device: str = "cpu",
) -> np.ndarray:
    """モデルで予測を実行する。"""
    model.eval()
    with torch.no_grad():
        X_tensor = torch.tensor(X, dtype=torch.float32).to(device)
        pred = model(X_tensor)
    pred_np: np.ndarray = pred.cpu().numpy()
    return pred_np


def evaluate(
    model: ThreatLSTM,
    X_test: np.ndarray,
    y_test: np.ndarray,
    device: str = "cpu",
) -> dict[str, float]:
    """モデルを評価する。"""
    predictions = predict(model, X_test, device)
    mse = float(np.mean((predictions - y_test) ** 2))
    mae = float(np.mean(np.abs(predictions - y_test)))
    return {"mse": mse, "mae": mae}


def save_model(model: ThreatLSTM, path: str) -> None:
    """モデルを保存する。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    torch.save(model.state_dict(), path)
    logger.info(f"[LSTM] Model saved to {path}")


def load_model(path: str, input_dim: int) -> ThreatLSTM:
    """モデルを読み込む。"""
    model = ThreatLSTM(input_dim)
    model.load_state_dict(torch.load(path, weights_only=True, map_location="cpu"))
    model.eval()
    return model

# self-proving-observation/
# └── proposed/
#     └── receiver/
#         └── Dockerfile  — FastAPI 受付 + ワーカー

FROM python:3.14-slim

WORKDIR /app

COPY proposed/receiver/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# receiver/worker は src.common.clickhouse・src.logging_config に依存するため、
# src/receiver/ だけでなく src/ パッケージ全体をそのままの階層でコピーする。
COPY src/ ./src/

CMD ["uvicorn", "src.receiver.app:app", "--host", "0.0.0.0", "--port", "8000"]

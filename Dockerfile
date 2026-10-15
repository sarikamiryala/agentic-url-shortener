FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --create-home app && mkdir -p /data && chown -R app /data /app
USER app
EXPOSE 8000
# One worker: run state and the rate limiter are in-process (see docs/TESTING_AND_TRADEOFFS.md).
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

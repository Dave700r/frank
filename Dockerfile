FROM python:3.12-slim

# matrix-nio[e2e] needs libolm
RUN apt-get update && \
    apt-get install -y --no-install-recommends libolm-dev gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

# Data and config are mounted at runtime
VOLUME ["/app/data", "/app/config.yaml"]

CMD ["python", "matrix_bot.py"]

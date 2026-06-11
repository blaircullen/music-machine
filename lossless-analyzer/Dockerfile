FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy scipy httpx mutagen

WORKDIR /app

COPY lossless-analyzer/worker.py /app/worker.py
COPY backend/lossless_detect.py /app/lossless_detect.py
COPY tools/recue_fakes.py /app/recue_fakes.py

CMD ["python3", "/app/worker.py"]

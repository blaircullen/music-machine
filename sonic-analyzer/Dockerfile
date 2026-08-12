FROM ghcr.io/mtg/essentia:latest

# Install Python 3 (image has Python 2 and Python 3 available; install pip3 explicitly)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY init_models.sh /app/init_models.sh
COPY worker.py /app/worker.py

RUN chmod +x /app/init_models.sh

# Run init (download/verify models) then start worker
CMD ["/bin/bash", "-c", "/app/init_models.sh && python3 /app/worker.py"]

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    MUSICARR_CONFIG=/config MUSICARR_MUSIC_DIR=/music MUSICARR_PORT=8689 \
    PUID=1000 PGID=1000
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && apt-get update && apt-get install -y --no-install-recommends gosu tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY app ./app
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8689
VOLUME ["/config"]
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8689/ping')" || exit 1
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.main"]

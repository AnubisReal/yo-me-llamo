FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    YML_HOST=0.0.0.0 YML_PORT=8787 \
    YML_DATA_DIR=/data YML_TEMP_DIR=/work \
    YML_LIBRARY_ROOT=/extraible2/Series YML_OUTPUT_DIR="/extraible2/Series/yo me llamo"
COPY app.py library.py config.json ./
COPY static/ ./static/
RUN mkdir -p /data /work /extraible2/Series && chown -R 1000:1000 /app /data /work /extraible2
USER 1000:1000
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=4)"
CMD ["python", "app.py"]

FROM python:3.13-slim

# 取引日の表示は UTC → ローカル時刻の変換に依存するため、タイムゾーン情報を入れておく
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tokyo \
    GNUCASH_HOST=0.0.0.0 \
    GNUCASH_PORT=8000

WORKDIR /app
COPY web/ ./web/

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3)"

# GnuCash ファイルは /data にマウントし、GNUCASH_FILE で指定する（compose.yaml を参照）
CMD ["python", "web/server.py"]

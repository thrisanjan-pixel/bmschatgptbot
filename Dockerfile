FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BMS_DB=/data/state.sqlite3 BMS_PUBLIC=1 SCREENWATCH_PASSWORDLESS=1 HOME=/home/app BMS_ROWS_ENABLED=1 BMS_REQUEST_BUDGET=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates gosu tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /data && chown app:app /data
# Litestream streams the SQLite database to Cloudflare R2 (see litestream.yml). Inert until
# LITESTREAM_* variables are set. TARGETARCH is amd64 on Railway, arm64 on an Oracle Ampere VM.
ARG LITESTREAM_VERSION=0.3.13
ARG TARGETARCH=amd64
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-${TARGETARCH}.deb /tmp/litestream.deb
RUN dpkg -i /tmp/litestream.deb && rm /tmp/litestream.deb && litestream version
COPY litestream.yml /etc/litestream.yml
WORKDIR /app
COPY requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt
COPY --chown=app:app http_source.py api_watch.py api_source.py store.py telegram.py local_credentials.py dashboard.py supervise.py watch_preferences.py row_watch.py cloud_row_reader.py seat_map.py health_watch.py backup.py role.py failover.py ntfy.py ./
COPY --chown=app:app static/screenwatch.html static/manifest.json static/icon-180.png static/icon-192.png static/icon-512.png ./static/
COPY --chmod=755 deploy/railway-entrypoint.sh /usr/local/bin/bms-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/bms-entrypoint"]
CMD ["python", "-u", "supervise.py", "--api"]

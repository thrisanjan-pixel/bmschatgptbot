FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BMS_DB=/data/state.sqlite3 BMS_PUBLIC=1 HOME=/home/app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates gosu tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY --chown=app:app api_watch.py api_source.py store.py telegram.py local_credentials.py dashboard.py supervise.py ./
COPY --chown=app:app static/screenwatch.html ./static/screenwatch.html
COPY --chmod=755 deploy/railway-entrypoint.sh /usr/local/bin/bms-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/bms-entrypoint"]
CMD ["python", "-u", "supervise.py", "--api"]

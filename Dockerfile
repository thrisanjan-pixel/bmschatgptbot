FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BMS_DB=/data/state.sqlite3 BMS_PUBLIC=1 HOME=/home/app PLAYWRIGHT_BROWSERS_PATH=/opt/playwright BMS_ROWS_ENABLED=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates gosu tini xvfb xauth \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY requirements-browser.txt ./
RUN pip install --no-cache-dir -r requirements-browser.txt \
    && python -m playwright install --with-deps --no-shell chromium \
    && chmod -R a+rX /opt/playwright \
    && rm -rf /var/lib/apt/lists/*
COPY --chown=app:app api_watch.py api_source.py store.py telegram.py local_credentials.py dashboard.py supervise.py watch_preferences.py row_watch.py row_reader.py row_agent_protocol.py ./
COPY --chown=app:app static/screenwatch.html ./static/screenwatch.html
COPY --chmod=755 deploy/railway-entrypoint.sh /usr/local/bin/bms-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/bms-entrypoint"]
CMD ["python", "-u", "supervise.py", "--api"]

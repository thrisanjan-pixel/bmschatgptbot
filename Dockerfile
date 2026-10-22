FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    CHROMIUM_PATH=/usr/bin/chromium \
    BMS_DB=/data/state.sqlite3 \
    HOME=/home/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates fonts-liberation gosu tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /data \
    && chown app:app /data

WORKDIR /app
COPY requirements-browser.txt ./
RUN pip install --disable-pip-version-check -r requirements-browser.txt
COPY --chown=app:app city_watch.py source.py parsers.py seats.py store.py telegram.py ./
COPY --chmod=755 deploy/railway-entrypoint.sh /usr/local/bin/bms-entrypoint

# Fix the volume's mount-directory ownership, then run as the app user.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/bms-entrypoint"]
CMD ["python", "-u", "city_watch.py"]

#!/bin/sh
set -eu
test -d /data
chown app:app /data
# With R2 configured, Litestream wraps the bot and streams /data/state.sqlite3 to
# s3://$LITESTREAM_BUCKET/$BOT_ROLE/. Without it the bot starts exactly as before.
if [ -n "${LITESTREAM_BUCKET:-}" ] && [ -n "${LITESTREAM_ACCESS_KEY_ID:-}" ] && [ -n "${LITESTREAM_SECRET_ACCESS_KEY:-}" ] && [ -n "${LITESTREAM_ENDPOINT:-}" ]; then
  export LITESTREAM_REPLICA_PATH="${LITESTREAM_REPLICA_PATH:-${BOT_ROLE:-primary}}"
  export LITESTREAM_WRAPPED=1
  # A new, empty disk gets this instance's own replica back before the bot starts.
  gosu app litestream restore -config /etc/litestream.yml -if-db-not-exists -if-replica-exists "$BMS_DB" \
    || echo "Litestream restore skipped; starting with the local database"
  exec gosu app litestream replicate -config /etc/litestream.yml -exec "$*"
fi
exec gosu app "$@"

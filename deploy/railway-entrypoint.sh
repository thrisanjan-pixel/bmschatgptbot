#!/bin/sh
set -eu
test -d /data
chown app:app /data
exec gosu app "$@"

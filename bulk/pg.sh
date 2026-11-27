#!/usr/bin/env bash
# Manage the private PostgreSQL cluster used by the bulk-data playground.
#
# Why not Docker: docker isn't installed here and installing it needs root.
# initdb can create a cluster owned by any user, so we get container-like
# isolation with zero privileges. Nothing here touches the system cluster
# on port 5432.
set -euo pipefail

PGBIN=/usr/lib/postgresql/16/bin
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PGDATA="${PGDATA:-$HERE/pgdata}"
PGPORT="${PGPORT:-55432}"
DBNAME="${DBNAME:-eventflow_bulk}"
PGUSER="${PGUSER:-rahul}"
LOG="$PGDATA/server.log"

dsn() { printf 'postgresql://%s@/%s?host=%s&port=%s' "$PGUSER" "$DBNAME" "$PGDATA" "$PGPORT"; }

case "${1:-status}" in
  init)
    if [ -d "$PGDATA" ]; then
      echo "already initialised: $PGDATA"; exit 0
    fi
    mkdir -p "$PGDATA"
    "$PGBIN/initdb" -D "$PGDATA" -U "$PGUSER" --auth=trust -E UTF8
    # Bulk-load tuning. This cluster holds disposable data only.
    cat >>"$PGDATA/postgresql.conf" <<EOF

# --- bulk-data playground ---
port = $PGPORT
listen_addresses = ''
unix_socket_directories = '$PGDATA'
shared_buffers = 512MB
work_mem = 32MB
maintenance_work_mem = 256MB
max_wal_size = 4GB
checkpoint_timeout = 30min
synchronous_commit = off
fsync = off
full_page_writes = off
EOF
    echo "initialised $PGDATA on port $PGPORT"
    ;;

  start)
    if "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
      echo "already running on port $PGPORT"; exit 0
    fi
    "$PGBIN/pg_ctl" -D "$PGDATA" -l "$LOG" -w start
    "$PGBIN/createdb" -h "$PGDATA" -p "$PGPORT" -U "$PGUSER" "$DBNAME" 2>/dev/null \
      || echo "database $DBNAME already exists"
    echo "running: $(dsn)"
    ;;

  stop)
    "$PGBIN/pg_ctl" -D "$PGDATA" -w stop
    ;;

  restart) "$0" stop; "$0" start ;;

  status)
    if "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
      echo "UP   $(dsn)"
      "$PGBIN/psql" -h "$PGDATA" -p "$PGPORT" -U "$PGUSER" -d "$DBNAME" -Atc \
        "select 'rows in largest tables: '||sum(n_live_tup) from pg_stat_user_tables"
    else
      echo "DOWN $PGDATA (run: $0 start)"
    fi
    ;;

  dsn) dsn ;;

  *)
    echo "usage: $0 {init|start|stop|restart|status|dsn}" >&2
    exit 2
    ;;
esac
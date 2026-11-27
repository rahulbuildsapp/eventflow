#!/usr/bin/env bash
# One command per task for the EventFlow bulk-data playground.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="$HERE/venv/bin/python"
PG="$HERE/pg.sh"
DSN="$("$PG" dsn)"

usage() {
  cat <<'EOF'
Usage: ./bulk/ctl.sh <command> [args]

  setup [scale]          create DB, migrate, load data, link photos (one-time)
  start|stop|restart|status   manage the private database
  load [scale] [mess]    reload data (defaults: 1.0 1.0)
  sql [args...]          psql prompt, or e.g. sql -c "SELECT ..."
  shell                  Django shell on the bulk DB
  serve [addr]           Django dev server on the bulk DB (default 127.0.0.1:8000)
  exercises [args...]    12 naive-vs-correct SQL lessons
  orm                    ORM query-count / timing checks
  clear                  stop DB and delete generated data (pgdata + media)
  help                   this message

Examples:
  ./bulk/ctl.sh setup
  ./bulk/ctl.sh sql -c "SELECT count(*) FROM events_rsvp;"
  ./bulk/ctl.sh exercises --only 11 --explain
  ./bulk/ctl.sh load 5 0        # ~1.5M rows, clean data
EOF
}

ensure_python() {
  if [ -x "$PY" ]; then
    return
  fi
  echo "Creating bulk virtualenv (first run only)..."
  python3 -m venv "$HERE/venv"
  "$HERE/venv/bin/pip" install --upgrade pip >/dev/null
  "$HERE/venv/bin/pip" install -r "$ROOT/requirements/requirements.txt"
}

photos() {
  if compgen -G "$HERE/media/_pool/photo_*.jpg" > /dev/null; then
    "$PY" "$HERE/fetch_images.py" --reuse
  else
    "$PY" "$HERE/fetch_images.py" --source local
  fi
}

cmd="${1:-help}"
if [ "$#" -gt 0 ]; then shift; fi

case "$cmd" in
  setup)
    ensure_python
    "$PG" init
    "$PG" start
    (cd "$ROOT" && "$PY" manage.py migrate --settings=rsvp.settings_bulk)
    "$PY" "$HERE/verify_schema.py"
    "$PY" "$HERE/generate.py" --dsn "$DSN" --scale "${1:-1.0}" --truncate
    photos
    "$PY" "$HERE/make_admin.py" --password admin123 >/dev/null || true
    "$PG" status
    echo "Ready. Try: ./bulk/ctl.sh exercises"
    ;;
  start|stop|restart|status|dsn)
    "$PG" "$cmd" "$@"
    ;;
  load)
    ensure_python
    "$PY" "$HERE/generate.py" --dsn "$DSN" \
      --scale "${1:-1.0}" --mess "${2:-1.0}" --truncate
    photos
    ;;
  sql)
    exec "$HERE/psql" "$@"
    ;;
  shell)
    ensure_python
    (cd "$ROOT" && exec "$PY" manage.py shell --settings=rsvp.settings_bulk "$@")
    ;;
  serve)
    ensure_python
    addr="${1:-127.0.0.1:8000}"
    (cd "$ROOT" && exec "$PY" manage.py runserver --settings=rsvp.settings_bulk "$addr")
    ;;
  exercises)
    ensure_python
    "$PY" "$HERE/exercises.py" "$@"
    ;;
  orm)
    ensure_python
    "$PY" "$HERE/orm_check.py"
    ;;
  clear)
    "$PG" stop || true
    rm -rf "$HERE/pgdata" "$HERE/media/event_img"
    echo "Data cleared (photo pool kept). Recreate with: ./bulk/ctl.sh setup"
    ;;
  help|*)
    usage
    ;;
esac

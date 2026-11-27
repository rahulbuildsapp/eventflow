# Bulk data playground — quick guide

Everything lives in this one `bulk/` folder: the database, the photos, the
Python environment and the scripts. Nothing touches your real dev database.
To remove it all: `./bulk/ctl.sh stop && rm -rf bulk` from the project root.

## 1. Set up (once)

```bash
./bulk/ctl.sh setup
```

On the first run it also creates `bulk/venv` and installs the project
requirements. That does four things: creates a private PostgreSQL cluster in
`bulk/pgdata` (port 55432, Unix socket only), runs Django migrations, loads
~300K messy rows with `COPY`, and links the photos in `bulk/media`. It ends
with a data-quality audit — the "defects" it prints are intentional.

## 2. Play with it

```bash
./bulk/ctl.sh status                                  # is the DB up?
./bulk/ctl.sh sql -c "SELECT count(*) FROM events_rsvp;"   # raw SQL
./bulk/ctl.sh sql                                     # interactive psql
./bulk/ctl.sh exercises                               # 12 naive-vs-correct lessons
./bulk/ctl.sh exercises --only 11 --explain           # one lesson + query plan
./bulk/ctl.sh orm                                     # N+1 counts, prefetch, Subquery
./bulk/ctl.sh shell                                   # Django ORM shell
./bulk/ctl.sh serve                                   # http://127.0.0.1:8000/
```

The server uses `rsvp.settings_bulk`, so it reads the bulk DB and needs no
Redis, Celery worker or SMTP. Data is already loaded — you don't need to load
it again unless you want a different scale/mess.

In the shell, `DEBUG=True`, so this shows the SQL a queryset actually ran:

```python
from django.db import connection, reset_queries
from events.models import Event
reset_queries()
list(Event.objects.all()[:10])
print(len(connection.queries))
```

## 3. Reload / reset

```bash
./bulk/ctl.sh load              # same data again (wipes first)
./bulk/ctl.sh load 5 0          # ~1.5M rows, clean (no deliberate mess)
./bulk/ctl.sh clear             # stop DB + delete pgdata/ and media/event_img/
./bulk/ctl.sh setup             # rebuild after clear
```

For a custom seed, call the generator directly:
`./bulk/venv/bin/python bulk/generate.py --dsn "$(./bulk/pg.sh dsn)" --seed 999 --truncate`

## What's in the box

| Path | Purpose |
|---|---|
| `ctl.sh` | the only command you need |
| `pg.sh`, `psql` | private cluster control / SQL prompt |
| `generate.py` | ~300K rows via `COPY`, plus deliberate mess and an audit |
| `exercises.py` | 12 SQL questions where naive and correct answers disagree |
| `orm_check.py` | same lessons through the ORM, with query counts and timings |
| `fetch_images.py` | links a small photo pool to all 6000 image paths |
| `verify_schema.py` | checks `COPY` targets against migrated schema |
| `make_admin.py` | creates `admin` / `admin123` for `/admin/` |
| `pgdata/`, `media/`, `venv/` | generated locally; ignored by git, safe to delete |

## Things worth knowing

- Always use `--settings=rsvp.settings_bulk` (the `ctl.sh shell` command does
  this for you). `rsvp.settings` points at your real database.
- The cluster has no TCP listener; the host is a socket path inside `bulk/`.
- `fsync=off` — this database is disposable, not for anything you care about.
- The mess is the point: cached `EventAnalytics` counters are wrong, cancelled
  RSVPs count as attendees, `N/A` phones pass `IS NOT NULL`, etc.
- Deeper background: `ADVANCED_ORM_STUDY_GUIDE.md` in the project root.

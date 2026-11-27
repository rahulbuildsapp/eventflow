"""Verify every COPY target in generate.py matches the migrated Django schema.

Fails loudly on a missing table or column instead of silently loading garbage.
"""
import importlib.util
import pathlib
import sys

import psycopg

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gen", HERE / "generate.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

DSN = ("postgresql://rahul@/eventflow_bulk"
       f"?host={HERE / 'pgdata'}&port=55432")

problems = []
with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    tables = {r[0] for r in cur.fetchall()}

    for table, cols in gen.COPY_TARGETS.items():
        if table not in tables:
            problems.append(f"MISSING TABLE  {table}")
            continue
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s", (table,))
        real = {r[0] for r in cur.fetchall()}
        missing = [c for c in cols if c not in real]
        if missing:
            problems.append(f"MISSING COLUMNS {table}: {missing}")

    # auth tables migrations create empty; confirm they are reachable
    for t in ("auth_permission", "django_content_type", "roles"):
        if t not in tables:
            problems.append(f"MISSING TABLE  {t}")

if problems:
    print("SCHEMA MISMATCH:")
    for p in problems:
        print("  " + p)
    sys.exit(1)

n_tables = len(gen.COPY_TARGETS)
n_cols = sum(len(v) for v in gen.COPY_TARGETS.values())
print(f"OK: {n_tables} COPY targets, {n_cols} columns, all present in DB.")
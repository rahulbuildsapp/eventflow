"""
Hands-on exercises against the messy EventFlow dataset.

Each exercise states a question, the naive query most people write first, and
the correct answer computed independently (usually a second SQL formulation).
The point is to make the *disagreement* visible: the naive query silently
returns wrong numbers on this dataset, and that is the lesson.

Run:
    bulk/venv/bin/python bulk/exercises.py
    bulk/venv/bin/python bulk/exercises.py --only 3
"""

from __future__ import annotations

import argparse
import pathlib
import textwrap
import time

import psycopg

DSN = ("postgresql://rahul@/eventflow_bulk"
       f"?host={pathlib.Path(__file__).resolve().parent / 'pgdata'}&port=55432")


class Runner:
    def __init__(self, conn):
        self.conn = conn

    def scalar(self, sql: str, params=None):
        with self.conn.cursor() as c:
            c.execute(sql, params)
            row = c.fetchone()
            return row[0] if row else None

    def timed(self, sql: str, params=None):
        t0 = time.perf_counter()
        val = self.scalar(sql, params)
        return val, (time.perf_counter() - t0) * 1000

    def timed_rows(self, sql: str, params=None):
        """For queries whose interesting output is several rows."""
        t0 = time.perf_counter()
        with self.conn.cursor() as c:
            c.execute(sql, params)
            rows = c.fetchall()
        return rows, (time.perf_counter() - t0) * 1000


# Each exercise: (title, why-it-bites, naive_sql, correct_sql, note)
EXERCISES = [
    (
        "1. Counting RSVPs per event",
        "`COUNT(*)` includes cancelled RSVPs. Most dashboards want only "
        "live registrations.",
        "SELECT count(*) FROM events_rsvp",
        "SELECT count(*) FROM events_rsvp WHERE NOT is_cancelled",
        "The naive version counts refunds and cancellations as attendees.",
    ),
    (
        "2. Revenue per event: which table do you trust?",
        "`RSVP.total_charge` and `payments_transaction.amount` disagree on "
        "thousands of rows -- deliberately. One is revenue, the other is what "
        "the gateway actually captured.",
        "SELECT sum(t.amount) FROM payments_transaction t "
        "JOIN events_tickettype tt ON tt.id = t.ticket_type_id "
        "JOIN events_event e ON e.id = tt.event_id "
        "WHERE t.payment_status = 'SUCCESS'",
        "SELECT sum(r.total_charge) FROM events_rsvp r "
        "WHERE r.is_completed",
        "Neither is 'the' answer -- they disagree by design. Always say which "
        "one a report means.",
    ),
    (
        "3. The cached analytics counter",
        "`EventAnalytics.total_resvps` is a Celery-maintained cache. It is "
        "intentionally stale. Compare it against reality.",
        "SELECT sum(total_rsvps) FROM analytics_eventanalytics",
        "SELECT count(*) FROM events_rsvp",
        "If these differ by a wide margin, any dashboard reading the cache is "
        "showing numbers that do not exist.",
    ),
    (
        "4. 'Active' RSVPs -- is the column trustworthy?",
        "`RSVP.save()` recomputes `is_active` from the other flags, but bulk "
        "rows bypassed save(), so the stored column is stale.",
        "SELECT count(*) FROM events_rsvp WHERE is_active",
        "SELECT count(*) FROM events_rsvp "
        "WHERE NOT (is_cancelled OR is_completed OR is_expired)",
        "Filtering on a derived column that was never recomputed is a classic "
        "production bug.",
    ),
    (
        "5. Impossible status combinations",
        "`is_cancelled AND is_attended AND is_completed` all true. The "
        "`status` property checks is_cancelled first, so these hide.",
        "SELECT count(*) FROM events_rsvp WHERE is_attended",
        "SELECT count(*) FROM events_rsvp "
        "WHERE is_attended AND NOT is_cancelled",
        "An attendance report that doesn't exclude cancelled rows over-counts.",
    ),
    (
        "6. Attendees with a real email address",
        "`IS NOT NULL` / `<> ''` is not enough: many phones are `'N/A'`, "
        "`'NULL'`, `'-'` or whitespace.",
        "SELECT count(*) FROM user_profiles_customuser "
        "WHERE phone IS NOT NULL AND phone <> ''",
        "SELECT count(*) FROM user_profiles_customuser "
        "WHERE phone ~ '^[0-9+][0-9 ()-]{6,}$'",
        "Real-world 'has a phone number' tests need a shape check, not a "
        "null check.",
    ),
    (
        "7. Case-insensitive email lookup",
        "Emails differ only by case and surrounding spaces. A plain `=` "
        "lookup misses rows a human would consider the same person.",
        # id=6 is stored with surrounding spaces -- a literal match finds nothing
        "SELECT count(*) FROM user_profiles_customuser "
        "WHERE email = ' deepa.kapoor6@example.com '",
        "SELECT count(*) FROM user_profiles_customuser "
        "WHERE lower(btrim(email)) = 'deepa.kapoor6@example.com'",
        "Add a functional index on lower(btrim(email)) if this is hot.",
    ),
    (
        "8. Distinct purchasers per event",
        "Duplicate transaction rows exist with the same user + ticket type. "
        "`COUNT(DISTINCT ...)` and plain `COUNT` disagree.",
        "SELECT count(*) FROM payments_transaction "
        "WHERE ticket_type_id IN (SELECT id FROM events_tickettype "
        "WHERE event_id = 1)",
        "SELECT count(DISTINCT user_id) FROM payments_transaction "
        "WHERE ticket_type_id IN (SELECT id FROM events_tickettype "
        "WHERE event_id = 1)",
        "Decide deliberately whether you want rows or people.",
    ),
    (
        "9. Sold-out detection",
        "`quantity_available = 0` does not mean nobody bought a ticket -- "
        "RSVPs reference those ticket types anyway.",
        "SELECT count(*) FROM events_tickettype WHERE quantity_available = 0",
        "SELECT count(*) FROM events_tickettype t "
        "WHERE t.quantity_available = 0 AND EXISTS ("
        "  SELECT 1 FROM events_rsvp r "
        "  JOIN payments_transaction p ON p.rsvp_id = r.id "
        "  WHERE p.ticket_type_id = t.id)",
        "Negative inventory is impossible (CHECK >= 0), so 'oversold' has to be "
        "detected from the RSVP side.",
    ),
    (
        "10. Support tickets resolved before they were created",
        "`resolved_at < created_at` -- backdated rows from a bad migration.",
        "SELECT count(*) FROM support_supportticket "
        "WHERE status = 'Closed'",
        "SELECT count(*) FROM support_supportticket "
        "WHERE status = 'Closed' AND resolved_at >= created_at",
        "Any 'time to resolution' average is skewed by these.",
    ),
    (
        "11. Top events by revenue (index-friendly formulation)",
        "A Subquery-per-row vs a single GROUP BY join -- compare timings.",
        "SELECT sum(rev) FROM ("
        "  SELECT e.id, e.name, sum(r.total_charge) AS rev "
        "  FROM events_event e JOIN events_rsvp r ON r.event_id = e.id "
        "  WHERE r.is_completed GROUP BY e.id, e.name "
        "  ORDER BY rev DESC LIMIT 10) x",
        "SELECT sum(rev) FROM ("
        "  SELECT event_id, sum(total_charge) AS rev FROM events_rsvp "
        "  WHERE is_completed GROUP BY event_id ORDER BY rev DESC LIMIT 10) x",
        "Results are identical -- that's the point. The win is ~1.5x from "
        "not joining events_event you don't need. If these two ever DISAGREE, "
        "you have a GROUP BY fan-out bug (joining a one-to-many relation).",
    ),
    (
        "12. Monthly signup trend",
        "Fat-tail timestamps: most rows are recent. Check whether the index "
        "on created_at is actually used before trusting the plan.",
        "SELECT date_trunc('month', created_at) AS m, count(*) "
        "FROM events_rsvp GROUP BY m ORDER BY m",
        "SELECT date_trunc('month', created_at) AS m, count(*) "
        "FROM events_rsvp WHERE created_at >= now() - interval '90 days' "
        "GROUP BY m ORDER BY m",
        "On append-mostly, heavily-skewed timestamps BRIN often beats B-tree.",
    ),
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dsn", default=DSN)
    p.add_argument("--only", type=int, default=None,
                   help="run a single exercise by number")
    p.add_argument("--explain", action="store_true",
                   help="also show EXPLAIN for the correct query")
    args = p.parse_args()

    # autocommit matters here: exercise 2 deliberately uses a query bad enough
    # to time out, and inside a transaction that abort would poison every
    # later exercise with "current transaction is aborted".
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        conn.execute("SET statement_timeout = '60s'")
        r = Runner(conn)

        for num, (title, why, naive, correct, note) in enumerate(EXERCISES, 1):
            if args.only and num != args.only:
                continue
            print("=" * 78)
            print(f"EXERCISE {num}: {title}")
            print("=" * 78)
            print(textwrap.fill(why, 76, initial_indent="  ", subsequent_indent="  "))
            print()

            def safe(sql):
                try:
                    return r.timed(sql)
                except Exception as e:
                    return f"ERROR: {str(e).splitlines()[0]}", 0.0

            n_val, n_ms = safe(naive)
            c_val, c_ms = safe(correct)

            print(f"  naive   ({n_ms:7.1f} ms): {n_val}")
            print(f"  correct ({c_ms:7.1f} ms): {c_val}")
            if n_val == c_val:
                print("  => agree (this dataset may not exercise this one "
                      "at the current scale)")
            else:
                print("  => DISAGREE -- the naive query is the trap")
            print()
            print(textwrap.fill(f"Note: {note}", 76,
                                initial_indent="  ", subsequent_indent="        "))

            if args.explain:
                with conn.cursor() as c:
                    c.execute(f"EXPLAIN (ANALYZE, BUFFERS) {correct}")
                    for (line,) in c.fetchall():
                        print("      " + line)
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
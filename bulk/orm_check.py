import os
import sys
import time

# Run from anywhere: put the project root on sys.path so `rsvp` imports, and
# chdir there so manage.py-style relative paths behave.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import django  # noqa: E402
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rsvp.settings_bulk")
django.setup()
from django.db import connection, reset_queries
from django.conf import settings
from events.models import Event, RSVP, TicketType
from payments.models import Transaction
from analytics.models import EventAnalytics
from user_profiles.models import CustomUser, Organizer

settings.DEBUG = True
reset_queries()

print("counts:", Event.objects.count(), RSVP.objects.count(),
      CustomUser.objects.count(), Transaction.objects.count())

# 1. N+1 demonstration -- the core lesson
reset_queries()
evs = list(Event.objects.all()[:50])
names = [e.organizer.organizer_name for e in evs]
print(f"N+1  : {len(connection.queries)} queries for 50 events")

reset_queries()
evs = list(Event.objects.select_related("organizer")[:50])
names = [e.organizer.organizer_name for e in evs]
print(f"sel_rel: {len(connection.queries)} queries for 50 events")

# 2. prefetch_related on reverse FK
reset_queries()
evs = list(Event.objects.prefetch_related("tickettype_set")[:50])
n = sum(len(e.tickettype_set.all()) for e in evs)
print(f"prefetch: {len(connection.queries)} queries, {n} ticket types")

# 3. the stale denormalized counter, through the ORM
a = EventAnalytics.objects.order_by("total_rsvps").first()
truth = RSVP.objects.filter(event_id=a.event_id).count()
print(f"analytics says total_rsvps={a.total_rsvps}, truth={truth}, "
      f"drift={a.total_rsvps - truth}")

# 4. aggregate vs annotate
from django.db.models import Count, Sum, Q, F
agg = RSVP.objects.filter(is_completed=True).aggregate(
    total=Sum("total_charge"), n=Count("id"))
print("aggregate:", agg)
from django.db.models import Subquery, OuterRef, Value, DecimalField
from django.db.models.functions import Coalesce

# "Top 3 events by revenue" -- and a Postgres gotcha worth internalising.
rev_sub = (RSVP.objects.filter(event_id=OuterRef("id"), is_completed=True)
           .values("event").annotate(t=Sum("total_charge")).values("t")[:1])

# WRONG: events with no completed RSVPs give rev = NULL, and in Postgres
# `ORDER BY rev DESC` sorts NULLs FIRST -- so this returns the three *worst*
# events, all with NULL revenue. Silent, and it looks plausible.
bad = list(Event.objects.annotate(rev=Subquery(rev_sub))
           .order_by("-rev").values("id", "rev")[:3])
print(f"  naive  top3 (rev={bad[0]['rev']!r}) <- NULLs sort first on DESC")

# RIGHT: wrap in Coalesce so events without revenue score 0, not NULL.
# Coalesce of a Decimal sum against an int literal needs an explicit
# output_field, else Django raises "Expression contains mixed types".
good = (Event.objects.annotate(rev=Coalesce(Subquery(rev_sub), Value(0),
                                            output_field=DecimalField(
                                                max_digits=14,
                                                decimal_places=2)))
        .order_by("-rev").values("id", "name", "rev")[:3])
for t in good:
    print(f"  fixed  top3: id={t['id']:<5} rev={t['rev']:>14,.2f}")

# 5. Subquery + OuterRef: compare a computed total against the cached one
sub = (RSVP.objects.filter(event_id=OuterRef("id"), is_completed=True)
       .values("event").annotate(t=Sum("total_charge")).values("t")[:1])
# NOTE: the reverse accessor for the EventAnalytics OneToOne is
# 'eventanalytics', not 'analytics' -- Django derives it from the model name.
rows = list(Event.objects
            .filter(eventanalytics__isnull=False)
            .annotate(real=Subquery(sub),
                      cached=F("eventanalytics__total_revenue"))
            .values("id", "name", "real", "cached")[:4])
for r in rows:
    rev, cach = r["real"] or 0, r["cached"] or 0
    print(f"  event {r['id']:<5} real={rev:>12,.2f} cached={cach:>12,.2f} "
          f"drift={cach - rev:>12,.2f}")

# 6. status property on an impossible row
bad = RSVP.objects.filter(is_cancelled=True, is_attended=True).first()
print("impossible flags -> RSVP.status returns:", repr(bad.status))

# 7. The reverse-accessor naming trap that just cost us a FieldError:
#    EventAnalytics' OneToOne is accessed as 'eventanalytics', and
#    .values() after .annotate() drops anything not listed -- a common
#    reason an annotation "disappears".
print("reverse accessors on Event:",
      sorted(f.name for f in Event._meta.get_fields()
             if f.auto_created and not f.concrete))

# 8. queryset timing at scale
t0=time.perf_counter(); RSVP.objects.filter(is_attended=True, is_cancelled=False).count()
print(f"timed filter+count: {(time.perf_counter()-t0)*1000:.1f} ms")

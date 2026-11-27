"""
Bulk dirty-data generator for EventFlow (PostgreSQL).

Purpose: build a large, deliberately messy dataset so the ORM / SQL
optimization patterns in ADVANCED_ORM_STUDY_GUIDE.md can be practiced
against realistic conditions (N+1s hurt, indexes matter, stale
denormalized counters lie, NULLs break naive assumptions).

Design notes
------------
* Writes with psycopg3 `COPY` (binary-ish text) not the ORM: the models
  have expensive save() overrides (RSVP.save does select_for_update and
  ticket decrementing) that would take hours at this volume and would
  silently "fix" the mess we want to preserve.
* Mess is parameterised by MESSINESS in [0.0, 1.0] so you can regenerate
  a clean-ish dataset for A/B comparison.
* Volume knobs at the top. Default ~300K rows total.

Usage
-----
    python generate.py --dsn "postgresql://user@host:port/db"
    python generate.py --scale 3.0          # ~1M rows
    python generate.py --mess 0.0           # clean baseline
    python generate.py --truncate           # wipe first
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit("psycopg[binary] is required:  pip install 'psycopg[binary]'")


# --------------------------------------------------------------------------
# Volume knobs (defaults sum to roughly 300K rows)
# --------------------------------------------------------------------------
N_ORGANIZERS = 400
N_EVENTS = 4_000
N_TICKET_TYPES = 12_000
N_USERS = 20_000
N_RSVPS = 120_000
N_TRANSACTIONS = 120_000
N_SUPPORT_TICKETS = 8_000
N_NOTIFICATIONS = 120_000
N_EVENT_IMAGES = 6_000

SEED = 20241001
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 30, 18, 0, tzinfo=IST)


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------
FIRST = [
    "aarav", "vivaan", "aditya", "vihaan", "arjun", "sai", "ananya", "diya",
    "ishita", "priya", "rohan", "sneha", "karan", "meera", "rahul", "pooja",
    "amit", "neha", "sanjay", "kavya", "nikhil", "riya", "sameer", "tanya",
    "harish", "nisha", "rahul", "sunita", "mohit", "anjali", "deepak", "kavita",
    "sanjay", "rekha", "arun", "shalini", "vikram", "swati", "manish", "asha",
    "kiran", "preeti", "ravi", "deepa", "gaurav", "shreya", "abhishek", "divya",
    "yash", "ruchi", "harsh", "pooja", "varun", "shruti", "ashwin", "kirti",
]
LAST = [
    "sharma", "verma", "patel", "reddy", "nair", "iyer", "gupta", "singh",
    "kumar", "mehta", "joshi", "desai", "chopra", "malhotra", "kapoor", "bose",
    "banerjee", "chatterjee", "mukherjee", "rao", "pillai", "krishnan", "menon",
]
CITIES = [
    ("Bengaluru", "IN"), ("Mumbai", "IN"), ("Delhi", "IN"), ("Hyderabad", "IN"),
    ("Chennai", "IN"), ("Pune", "IN"), ("Kolkata", "IN"), ("Ahmedabad", "IN"),
    ("Jaipur", "IN"), ("Kochi", "IN"), ("London", "GB"), ("Dublin", "IE"),
    ("Berlin", "DE"), ("Singapore", "SG"), ("Toronto", "CA"), ("Austin", "US"),
    ("Amsterdam", "NL"), ("Lisbon", "PT"), ("Remote", ""),
]
EVENT_WORDS_A = [
    "Tech", "Design", "Music", "Startup", "Health", "Finance", "AI", "Cloud",
    "Data", "DevOps", "Product", "Growth", "UX", "Security", "Open Source",
]
EVENT_WORDS_B = [
    "Summit", "Meetup", "Workshop", "Hackathon", "Conference", "Bootcamp",
    "Retreat", "Jam", "Forum", "Showcase", "Lab", "Night", "Camp", " Fest",
]
TICKET_NAMES = [
    "General", "Early Bird", "VIP", "Student", "Workshop Add-on", "Standing",
    "Balcony", "Backstage", "Press", "Speaker", "Volunteer", "Family Pack",
    "Day Pass", "Full Access", "Sponsor",
]
PAY_METHODS = ["Credit Card", "Debit Card", "Digital Payment"]
PAY_STATUS = ["SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "FAILURE", "PENDING"]
CURRENCIES = ["INR", "INR", "INR", "USD", "EUR"]
NOTIF_TYPES = ["rsvp_confirmed", "payment_receipt", "event_reminder",
               "event_cancelled", "refund_processed", "ticket_ready",
               "organizer_update", "promo"]
NOTIF_MSG = {
    "rsvp_confirmed": "Your RSVP for {event} is confirmed.",
    "payment_receipt": "Receipt for your payment to {event}.",
    "event_reminder": "{event} starts in 24 hours.",
    "event_cancelled": "{event} has been cancelled.",
    "refund_processed": "Refund issued for {event}.",
    "ticket_ready": "Your ticket for {event} is ready.",
    "organizer_update": "The organizer updated {event}.",
    "promo": "You have a new promo code for {event}.",
}
SUPPORT_SUBJ = [
    "Refund not received", "Cannot download ticket", "Double charged",
    "Event link broken", "Ticket transfer failed", "Unable to pay",
    "Wrong ticket name", "QR code not scanning", "Attendee not showing up",
    "Invoice request", "Cancel my RSVP", "Change ticket type",
]
MESSY_TEXT = [
    "  leading and trailing spaces  ",
    "MiXeD CaSe NaMe",
    "raghavmohan@example.com ",
    "raghavmohan@example.com",
    "Unknown attendee – no email on file",
    "Guest (walk-in)",
    "N/A",
    "n/a",
    "-",
    "NULL",
    "null",
    "अज्ञात प्रयोगकर्ता",
    " المستخدم المجهول",
    "_ZsBdK$#@random",
    "test@test.com",
    "unknown+placeholder@eventflow.invalid",
    "nil",
    "undefined",
    "TeSt@ExAmPlE.cOm",
    "  ",              # whitespace only
    "TODO: get real name",
    "Same as last order",
    "🚫🚫🚫",
    "café résumé naïve",
    "ＦＵＬＬＷＩＤＴＨ",
    "null\x0bbyte",            # vertical tab: legal in PG text, breaks naive .split()
]

# Zipf-ish popularity: a few organizers/events dominate, long tail behind.
ZIPF_SKEW = 1.15


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def zipf_weights(n: int, skew: float = ZIPF_SKEW) -> list[float]:
    """Relative popularity for each of n items; index 0 is the hottest."""
    raw = [1.0 / ((i + 1) ** skew) for i in range(n)]
    total = sum(raw)
    return [r / total for r in raw]


def messy_text(rng: random.Random, normal: str, prob: float) -> str | None:
    """Return a corrupted variant of `normal`, or None to keep it clean."""
    if rng.random() > prob:
        return normal
    choice = rng.random()
    if choice < 0.45:
        return rng.choice(MESSY_TEXT)
    if choice < 0.60:
        return normal.upper()
    if choice < 0.72:
        return normal.lower()
    if choice < 0.82:
        return unicodedata.normalize("NFKD", normal)
    if choice < 0.90:
        return normal + " " + rng.choice(MESSY_TEXT)
    if choice < 0.96:
        return " " * rng.randint(1, 4) + normal + " " * rng.randint(1, 4)
    return normal[: max(1, len(normal) // 2)]  # truncated


def messy_email(rng: random.Random, email: str) -> str:
    """
    Corrupt an email WITHOUT breaking uniqueness -- `email` carries a UNIQUE
    constraint, so we can only vary case/padding/typos, never replace it with a
    shared placeholder. This mirrors the real-world version of the problem:
    " Rahul@Example.com " and "rahul@example.com" are logically the same
    person but the database cannot know that.
    """
    r = rng.random()
    if r < 0.30:
        return email.upper()
    if r < 0.50:
        return email.lower()
    if r < 0.68:
        return " " + email + " "
    if r < 0.82:
        local, _, domain = email.partition("@")
        return f"{local}+tag{len(local)}@{domain}"
    if r < 0.92:
        local, _, domain = email.partition("@")
        return f"{local.replace('.', '_', 1)}@{domain.upper()}"
    return email


def money(rng: random.Random, lo: float, hi: float) -> str:
    return f"{rng.uniform(lo, hi):.2f}"


def skew_pick(rng: random.Random, weights: list[float], n: int) -> int:
    """Weighted index draw using an alias-free cumulative scan."""
    x = rng.random()
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if x <= acc:
            return min(i, n - 1)
    return n - 1


def weighted_table(rng: random.Random, values: list, weights: list[float]):
    cum, acc = [], 0.0
    for w in weights:
        acc += w
        cum.append(acc)
    x = rng.random() * acc
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if x <= cum[mid]:
            hi = mid
        else:
            lo = mid + 1
    return values[lo]


def fat_tail_ts(rng: random.Random, days_back: int = 540) -> datetime:
    """
    Timestamp with a fat right tail: most activity recent, a long tail of
    very old rows. Makes date-range indexes look useful and BRIN relevant.
    """
    u = rng.random()
    if u < 0.55:
        days = rng.randint(0, 30)          # recent burst
    elif u < 0.85:
        days = rng.randint(30, 180)
    else:
        days = rng.randint(180, days_back)
    secs = rng.randint(0, 86399)
    return NOW - timedelta(days=days, seconds=secs)


# --------------------------------------------------------------------------
# Schema notes
#
# We let Django create the schema via migrations (manage.py migrate), so the
# column lists below must match the models. Kept in one place so a model
# change breaks loudly here rather than silently producing garbage.
# --------------------------------------------------------------------------
COPY_TARGETS = {
    "roles": ["id", "name", "description", "created_at"],
    "user_profiles_customuser": [
        "id", "password", "last_login", "is_superuser", "username", "first_name",
        "last_name", "email", "is_staff", "is_active", "date_joined",
        "phone", "profession", "education", "goal", "languages", "address",
        "country", "image", "current_role_id", "created_at",
    ],
    "user_profiles_organizer": [
        "id", "user_id", "organizer_name", "organizer_email", "organizer_phone",
        "organizer_address", "organizer_slug", "organizer_image", "is_active",
        "created_at", "modified_at", "created_by_id", "modified_by_id",
    ],
    "events_event": [
        "id", "organizer_id", "name", "description", "description_html", "date",
        "location", "metadata", "is_virtual", "is_published", "is_active",
        "has_finished_event", "created_at", "modified_at",
    ],
    "events_eventimage": ["id", "event_id", "image"],
    "events_tickettype": [
        "id", "event_id", "name", "price", "currency", "image",
        "stripe_price_id", "quantity_available", "discount_code", "created_at",
    ],
    "events_rsvp": [
        "id", "transaction_id", "event_id", "attendee_id", "ticket_qty",
        "total_charge", "is_active", "is_attended", "is_completed",
        "is_cancelled", "is_refunded", "is_expired", "metadata", "created_at",
    ],
    "payments_transaction": [
        "id", "user_id", "rsvp_id", "ticket_type_id", "currency", "amount",
        "quantity", "payment_method", "transaction_id", "session_id",
        "payment_status", "billing_info", "created_at",
    ],
    "payments_eventpaymentbill": [
        "id", "organizer_id", "event_id", "currency", "amount",
        "stripe_event_bill_id", "has_finished_event", "has_completed_payment",
        "created_at",
    ],
    "payments_transactionoforganizer": [
        "id", "event_bill_id", "payment_date", "currency", "amount",
        "payment_method", "transaction_id", "payment_status", "billing_info",
        "created_at",
    ],
    "analytics_eventanalytics": [
        "id", "event_id", "total_rsvps", "total_revenue", "total_attended",
        "last_updated", "created_at",
    ],
    "analytics_tickettypeanalytics": [
        "id", "ticket_type_id", "total_sold", "total_revenue",
        "remaining_quantity", "last_updated", "created_at",
    ],
    "qr_codes_qrcode": ["id", "transaction_id", "code_image", "code_data",
                        "created_at"],
    "communication_notification": [
        "id", "user_id", "message", "created_at", "notification_type",
        "is_read", "link",
    ],
    "support_supportticket": [
        "id", "user_id", "subject", "message", "created_at", "priority",
        "status", "assigned_to_id", "resolution", "resolved_at",
    ],
    "user_profiles_userrsvphistory": ["id", "user_id", "rsvp_id", "created_at"],
    "user_profiles_customuser_available_roles": ["id", "customuser_id", "role_id"],
    "django_admin_log": [
        "id", "action_time", "user_id", "content_type_id", "object_id",
        "object_repr", "action_flag", "change_message",
    ],
}


# character_maximum_length per (table, column), read from the live schema so a
# model change can't silently drift from what the generator is allowed to emit.
_COL_LIMITS: dict[tuple[str, str], int] = {}


def load_column_limits(cur) -> None:
    cur.execute("""
        SELECT table_name, column_name, character_maximum_length
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND character_maximum_length IS NOT NULL
    """)
    _COL_LIMITS.clear()
    for table, col, length in cur.fetchall():
        _COL_LIMITS[(table, col)] = int(length)


def _scrub(table: str, col: str, v):
    """
    Make a generated value legal for its column:
      * Postgres text cannot store NUL (0x00) at all.
      * varchar(n) columns must be<= n chars -- dirty text like a pasted email
        address into `phone` is still useful mess, just truncated to fit.
    """
    if not isinstance(v, str):
        return v
    if "\x00" in v:
        v = v.replace("\x00", "")
    limit = _COL_LIMITS.get((table, col))
    if limit is not None and len(v) > limit:
        v = v[:limit]
    return v


def copy_in(cur, table: str, cols: list[str], rows: list[tuple]) -> int:
    """Stream rows into `table` via COPY. Empty list is a no-op."""
    if not rows:
        return 0
    stmt = f"COPY {table} ({', '.join(cols)}) FROM STDIN"
    with cur.copy(stmt) as cp:
        for r in rows:
            cp.write_row([_scrub(table, c_, v) for c_, v in zip(cols, r)])
    return len(rows)


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------
class Generator:
    def __init__(self, cur, args):
        self.cur = cur
        self.a = args
        self.m = args.mess
        self.rng = random.Random(args.seed)
        self.ids = {}          # table -> list of ids
        self._n = lambda base, n: max(1, int(base * n))  # noqa: E731

    # -- helpers ----------------------------------------------------------
    def maybe_null(self, p: float):
        return None if self.rng.random() < p * self.m else False

    def corrupt(self, s: str, p: float = 0.06) -> str | None:
        return messy_text(self.rng, s, p * self.m)

    def seed_roles(self):
        rows = [
            (1, "attendee", "Who attends events", NOW - timedelta(days=700)),
            (2, "organizer", "Who creates and manages events", NOW - timedelta(days=700)),
        ]
        copy_in(self.cur, "roles", COPY_TARGETS["roles"], rows)
        self.ids["roles"] = [1, 2]
        return 2

    def seed_users(self):
        n = self._n(N_USERS, self.a.scale)
        rng = self.rng
        # Some users have no phone/address/country at all (nullable blanks).
        pw = "pbkdf2_sha256$260000$" + "x" * 16 + "$" + "y" * 64
        rows = []
        for i in range(1, n + 1):
            fn = rng.choice(FIRST)
            ln = rng.choice(LAST)
            email = f"{fn}.{ln}{i}@example.com"
            if rng.random() < 0.22 * self.m:
                email = messy_email(rng, email)
            joined = fat_tail_ts(rng, 700)
            rows.append((
                i, pw, None, rng.random() < 0.01, f"{fn}{i}",
                self.corrupt(fn.capitalize(), 0.10) or "",
                self.corrupt(ln.capitalize(), 0.08) or "",
                email,
                rng.random() < 0.03,
                rng.random() < 0.88,                      # is_active
                joined,
                self.corrupt(f"+91{rng.randint(6000000000, 9999999999)}", 0.22)
                    if rng.random() > 0.18 * self.m else "",
                self.corrupt(self.corrupt(rng.choice(
                    ["Engineer", "Designer", "PM", "Student", "Founder",
                     "Analyst", "QA", "SRE"]), 0.10) or "", 0.05) or "",
                self.corrupt(rng.choice(["B.Tech", "B.Sc", "M.Tech", "MBA", "PhD"]),
                             0.15) or "",
                self.corrupt(rng.choice(["learn", "hire", "sell", "network"]),
                             0.15) or "",
                self.corrupt("Hindi,English", 0.12) or "",
                self.corrupt(f"{rng.randint(1, 999)} {rng.choice(['MG Road', 'Park St', 'Sector 21'])}, {rng.choice(CITIES)[0]}", 0.25) or "",
                self.corrupt(rng.choice(CITIES)[0], 0.10) or "",
                "",                                          # image
                rng.choice(self.ids["roles"]) if rng.random() < 0.7 else None,
                joined,
            ))
        rows.sort(key=lambda r: r[-1])
        copy_in(self.cur, "user_profiles_customuser",
                COPY_TARGETS["user_profiles_customuser"], rows)
        self.ids["users"] = list(range(1, n + 1))
        return n

    def seed_user_roles(self):
        rows = []
        for uid in self.ids["users"]:
            for rid in self.ids["roles"]:
                if self.rng.random() < 0.45:
                    rows.append((len(rows) + 1, uid, rid))
        copy_in(self.cur, "user_profiles_customuser_available_roles",
                COPY_TARGETS["user_profiles_customuser_available_roles"], rows)
        return len(rows)

    def seed_organizers(self):
        n = self._n(N_ORGANIZERS, self.a.scale)
        rng = self.rng
        rows = []
        for i in range(1, n + 1):
            uid = rng.choice(self.ids["users"])
            name = f"{rng.choice(EVENT_WORDS_A)} {rng.choice(['Collective', 'Labs', 'Ventures', 'Society', 'Guild', 'Hub'])}"
            if self.rng.random() < 0.15 * self.m:
                name = self.corrupt(name, 1.0) or name
            created = fat_tail_ts(rng, 600)
            rows.append((
                i, uid, name,
                messy_email(rng, f"org{i}@example.com")
                if rng.random() < 0.25 * self.m else f"org{i}@example.com",
                self.corrupt(f"+91{rng.randint(6000000000, 9999999999)}", 0.3) or "",
                self.corrupt(f"{rng.choice(CITIES)[0]}, India", 0.2) or "",
                # deliberately messy slugs: uppercase, spaces, empty, dupes
                rng.choice([
                    name.lower().replace(" ", "-"),
                    name.upper().replace(" ", "_"),
                    "",
                    "  ",
                    f"org-{i}",
                    name.lower().replace(" ", "-"),      # duplicate slug
                ]),
                "", True, created, created + timedelta(days=rng.randint(0, 90)),
                uid, rng.choice(self.ids["users"]) if rng.random() < 0.3 else None,
            ))
        rows.sort(key=lambda r: r[9])
        copy_in(self.cur, "user_profiles_organizer",
                COPY_TARGETS["user_profiles_organizer"], rows)
        self.ids["organizers"] = list(range(1, n + 1))
        return n

    def seed_events(self):
        n = self._n(N_EVENTS, self.a.scale)
        rng = self.rng
        # Skewed: a few organizers own most events.
        org_w = zipf_weights(len(self.ids["organizers"]))
        rows = []
        self.event_meta = []
        for i in range(1, n + 1):
            org = self.ids["organizers"][skew_pick(rng, org_w, len(org_w))]
            wa, wb = rng.choice(EVENT_WORDS_A), rng.choice(EVENT_WORDS_B)
            name = f"{wa} {wb} {rng.choice(['2024', '2025', '2026', 'Vol 3', 'Edition II'])}"
            if rng.random() < 0.12 * self.m:
                name = self.corrupt(name, 1.0) or name
            is_virtual = rng.random() < 0.30
            # Fat-tail event dates, clustered around "now".
            u = rng.random()
            if u < 0.25:
                date = NOW + timedelta(days=rng.randint(1, 120))
            elif u < 0.55:
                date = NOW - timedelta(days=rng.randint(1, 90))
            else:
                date = NOW - timedelta(days=rng.randint(90, 700))
            past = date < NOW
            published = rng.random() < 0.78
            active = published and not past and rng.random() < 0.85
            finished = past and rng.random() < 0.75
            city, cc = rng.choice(CITIES)
            md = None
            if rng.random() < 0.7:
                md = json.dumps({
                    "category": rng.choice(["Tech", "Design", "Music", "Health",
                                            "Finance", "Misc"]),
                    "is_virtual": is_virtual,
                    "capacity_hint": rng.choice([50, 100, 250, 500, None]),
                    "tags": rng.sample(["beginner", "advanced", "free", "sponsored",
                                        "recorded", "wifi"], k=rng.randint(0, 3)),
                    "source": rng.choice(["web", "mobile", "import", "admin", None]),
                })
            created = date - timedelta(days=rng.randint(10, 180))
            rows.append((
                i, org, name,
                f"{wa} event: {name}. " * rng.randint(1, 6),
                None if rng.random() < 0.4 else f"<p>{wa} {wb}</p>",
                date,
                None if is_virtual and rng.random() < 0.5
                else self.corrupt(f"{city}, {cc}", 0.15) or "",
                md, is_virtual, published, active, finished,
                created, date - timedelta(days=rng.randint(0, 9)),
            ))
            self.event_meta.append((i, date, published, finished))
        rows.sort(key=lambda r: r[12])
        copy_in(self.cur, "events_event", COPY_TARGETS["events_event"], rows)
        self.ids["events"] = list(range(1, n + 1))
        return n

    def seed_event_images(self):
        n = self._n(N_EVENT_IMAGES, self.a.scale)
        rng = self.rng
        rows = [(i, rng.choice(self.ids["events"]),
                 f"event_img/{rng.randint(2023, 2026)}/{rng.randint(1, 12):02d}/img_{i}.jpg")
                for i in range(1, n + 1)]
        copy_in(self.cur, "events_eventimage", COPY_TARGETS["events_eventimage"], rows)
        return n

    def seed_ticket_types(self):
        n = self._n(N_TICKET_TYPES, self.a.scale)
        rng = self.rng
        ev_w = zipf_weights(len(self.ids["events"]))
        rows = []
        self.tt_event = []
        cur = 1
        # ensure every event gets at least one ticket type
        plan = [(e, 0) for e in self.ids["events"]]
        extra = n - len(plan)
        for _ in range(max(0, extra)):
            plan.append((self.ids["events"][skew_pick(rng, ev_w, len(ev_w))], 0))
        for ev, _ in plan:
            i = cur
            cur += 1
            currency = rng.choice(CURRENCIES)
            base = {"INR": (299, 9999), "USD": (9, 299), "EUR": (8, 279)}[currency]
            price = money(rng, *base)
            if rng.random() < 0.05 * self.m:
                price = "0.00"          # free tier
            elif rng.random() < 0.03 * self.m:
                price = money(rng, -500, 0)   # negative price
            dc = None
            if rng.random() < 0.25:
                dc = rng.choice(["EARLY10", "VIP25", "SUMMER50", "  early10  ",
                                 "", "LAUNCH", "FIRST50"])
            rows.append((
                i, ev, self.corrupt(rng.choice(TICKET_NAMES), 0.10)
                or rng.choice(TICKET_NAMES),
                price, currency, "",
                f"price_{i}" if rng.random() < 0.8 else None,
                rng.choice([50, 100, 150, 200, 250, 500, 1000, 0]),
                dc, fat_tail_ts(rng, 500),
            ))
            self.tt_event.append((i, ev))
        rows.sort(key=lambda r: r[-1])
        copy_in(self.cur, "events_tickettype", COPY_TARGETS["events_tickettype"], rows)
        self.ids["ticket_types"] = [r[0] for r in rows]
        return len(rows)

    def seed_rsvps(self):
        n = self._n(N_RSVPS, self.a.scale)
        rng = self.rng
        ev_ids = self.ids["events"]
        ev_w = zipf_weights(len(ev_ids))
        tt_by_event = {}
        for tt, ev in self.tt_event:
            tt_by_event.setdefault(ev, []).append(tt)
        rows = []
        self.rsvp_meta = []   # (id, event_id, charge, flags) for downstream tables
        for i in range(1, n + 1):
            ev = ev_ids[skew_pick(rng, ev_w, len(ev_w))]
            tts = tt_by_event.get(ev) or [rng.choice(self.ids["ticket_types"])]
            attendee = rng.choice(self.ids["users"])
            qty = weighted = rng.choices(
                [1, 2, 3, 4, 5, 6, 8, 10, 25],
                weights=[62, 14, 8, 5, 4, 2, 2, 2, 1])[0]
            charge = money(rng, 0, 12000)

            # Status flags -- mostly coherent, corrupted when mess is high.
            r = rng.random()
            cancelled = r < 0.10
            expired = (not cancelled) and r < 0.16
            attended = (not cancelled and not expired) and r < 0.52
            completed = (not cancelled and not expired) and r < 0.74
            refunded = completed and rng.random() < 0.10
            if self.m > 0 and rng.random() < 0.16 * self.m:
                # impossible combos: cancelled AND attended AND completed
                cancelled, attended, completed = True, True, True
            elif self.m > 0 and rng.random() < 0.10 * self.m:
                # stale is_active: model says active is derived, so it lies
                completed, attended = True, False

            active = not (cancelled or completed or expired)
            if self.m > 0 and rng.random() < 0.12 * self.m:
                active = not active            # stale flag

            md = None
            if rng.random() < 0.35:
                md = json.dumps({
                    "ip": f"{rng.randint(1,223)}.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}",
                    "ua": rng.choice(["chrome", "safari", "firefox", None]),
                    "discount": rng.choice([None, "EARLY10", 0.15]),
                    "seat": rng.choice([None, f"{rng.randint(1,50)}-{rng.choice('ABCDE')}"]),
                })
            # NOTE: RSVP.transaction_id is declared `default=None` on the model
            # but the column is NOT NULL -- a real schema landmine. So it gets
            # blank/duplicate junk instead of NULL.
            txn = f"txn_{i}" if rng.random() < 0.9 else ""
            rows.append((
                i, txn, ev, attendee, qty, charge, active, attended, completed,
                cancelled, refunded, expired, md, fat_tail_ts(rng, 500),
            ))
            self.rsvp_meta.append((i, ev, charge, attended, completed,
                                   cancelled, attendee))
        rows.sort(key=lambda r: r[-1])
        copy_in(self.cur, "events_rsvp", COPY_TARGETS["events_rsvp"], rows)
        self.ids["rsvps"] = list(range(1, n + 1))
        return n

    def seed_transactions(self):
        n = self._n(N_TRANSACTIONS, self.a.scale)
        rng = self.rng
        rows = []
        rsvp_ids = self.ids["rsvps"]
        # OneToOne rsvp -> transaction; a few rsvps deliberately have none.
        rsvp_pool = list(rsvp_ids)
        if self.m > 0 and len(rsvp_pool) > 100:
            for _ in range(int(len(rsvp_pool) * 0.07 * self.m)):
                rsvp_pool.pop(rng.randrange(len(rsvp_pool)))
        tt_by_event = {}
        for tt, ev in self.tt_event:
            tt_by_event.setdefault(ev, []).append(tt)
        ev_of = {r[0]: r[1] for r in self.rsvp_meta}
        charge_of = {r[0]: r[2] for r in self.rsvp_meta}
        # index 6 is the attendee id (see self.rsvp_meta layout in seed_rsvps)
        user_of = {r[0]: r[6] for r in self.rsvp_meta}
        # duplicate transaction_id values (referential mess)
        dupe_pool = []

        # Transaction.rsvp is a OneToOne, so each rsvp_id may appear at most
        # once. When there are more transactions than RSVPs, the surplus rows
        # go in with rsvp_id = NULL (the column is nullable) instead of
        # duplicating a reference the database would reject.
        for i in range(1, n + 1):
            if rsvp_pool:
                rid = rsvp_pool.pop()
            else:
                rid = None
            ev = ev_of.get(rid) or rng.choice(self.ids["events"])
            tts = tt_by_event.get(ev) or [rng.choice(self.ids["ticket_types"])]
            tt = rng.choice(tts)
            status = rng.choice(PAY_STATUS)
            currency = rng.choice(CURRENCIES)
            amount = (charge_of.get(rid) if rid is not None else None) \
                or money(rng, 0, 9000)
            if rng.random() < 0.08 * self.m:
                amount = money(rng, -900, 0)          # negative/zero settlement
            if rng.random() < 0.06 * self.m:
                amount = money(rng, 0, 10)            # suspiciously tiny
            if rng.random() < 0.25 * self.m:
                dupe_pool.append(f"txn_dup_{rng.randint(1, 200)}")
            txn_id = (dupe_pool[-1] if dupe_pool and rng.random() < 0.3
                      else (f"ch_{rng.randint(10**17, 10**18 - 1)}"
                            if rng.random() < 0.92 else None))
            rows.append((
                i, user_of.get(rid) or rng.choice(self.ids["users"]), rid, tt,
                currency, amount, rng.randint(1, 10),
                rng.choice(PAY_METHODS) if rng.random() > 0.05 else None,
                txn_id,
                f"cs_test_{rng.randint(10**14, 10**15)}" if rng.random() < 0.85 else None,
                status,
                self.corrupt(json.dumps({
                    "country": rng.choice(CITIES)[0],
                    "postal": f"{rng.randint(100000, 999999)}",
                    "name": f"{rng.choice(FIRST).capitalize()} {rng.choice(LAST).capitalize()}",
                }), 0.12) if rng.random() < 0.6 else None,
                fat_tail_ts(rng, 500),
            ))
        rows.sort(key=lambda r: r[-1])
        copy_in(self.cur, "payments_transaction",
                COPY_TARGETS["payments_transaction"], rows)
        self.ids["transactions"] = [r[0] for r in rows]
        return len(rows)

    def seed_qrcodes(self):
        rng = self.rng
        tx_ids = self.ids["transactions"]
        rows = []
        for i, tid in enumerate(tx_ids, start=1):
            if rng.random() > 0.88:
                continue
            import uuid
            rows.append((
                str(uuid.UUID(int=rng.getrandbits(128), version=4)), tid, "",
                f"http://localhost:8000/ticket/{rng.randint(1, len(self.ids['rsvps']))}/",
                fat_tail_ts(rng, 500),
            ))
        copy_in(self.cur, "qr_codes_qrcode", COPY_TARGETS["qr_codes_qrcode"], rows)
        return len(rows)

    def seed_event_bills(self):
        rng = self.rng
        rows = []
        self.bill_of_event = {}
        ev_meta = {e[0]: e for e in self.event_meta}
        for i, ev in enumerate(self.ids["events"], start=1):
            if rng.random() > 0.62:
                continue
            m = ev_meta[ev]
            finished = m[3]
            amount = money(rng, 0, 50000)
            rows.append((
                i, rng.choice(self.ids["organizers"]), ev,
                rng.choice(CURRENCIES), amount,
                f"in_{rng.randint(10**10, 10**11)}" if rng.random() < 0.6 else None,
                finished,
                finished and rng.random() < 0.7,
                fat_tail_ts(rng, 500),
            ))
            self.bill_of_event[ev] = i
        copy_in(self.cur, "payments_eventpaymentbill",
                COPY_TARGETS["payments_eventpaymentbill"], rows)
        self.ids["bills"] = [r[0] for r in rows]
        return len(rows)

    def seed_organizer_transactions(self):
        rng = self.rng
        rows = []
        for i, bill in enumerate(self.ids["bills"], start=1):
            if rng.random() > 0.7:
                continue
            pd = fat_tail_ts(rng, 500)
            rows.append((
                i, bill, pd, rng.choice(CURRENCIES), money(rng, 0, 50000),
                rng.choice(PAY_METHODS),
                f"ch_org_{rng.randint(10**15, 10**16)}" if rng.random() < 0.9 else None,
                rng.choice(["SUCCESS", "SUCCESS", "PENDING", "FAILURE"]),
                None, pd + timedelta(days=rng.randint(0, 5)),
            ))
        copy_in(self.cur, "payments_transactionoforganizer",
                COPY_TARGETS["payments_transactionoforganizer"], rows)
        return len(rows)

    def seed_analytics(self):
        """Deliberately stale/wrong denormalized counters -- the good stuff."""
        rng = self.rng
        ev_rows, tt_rows = [], []
        real_rsvps = {}
        real_rev = {}
        for rid, ev, charge, attended, completed, cancelled, _uid in self.rsvp_meta:
            real_rsvps[ev] = real_rsvps.get(ev, 0) + 1
            if completed and charge:
                real_rev[ev] = real_rev.get(ev, 0.0) + float(charge)

        ev_dates = {e[0]: e[1] for e in self.event_meta}
        i = 0
        for ev in self.ids["events"]:
            i += 1
            if rng.random() > 0.85:
                continue                      # missing analytics row
            truth = real_rsvps.get(ev, 0)
            rev = real_rev.get(ev, 0.0)
            drift = 1.0
            if self.m > 0 and rng.random() < 0.65 * self.m:
                drift = rng.choice([0.0, 0.5, 1.5, rng.uniform(0.8, 1.2), 3.0])
            ev_rows.append((
                i, ev, int(truth * drift), f"{rev * drift:.2f}",
                int(truth * rng.uniform(0.0, 1.3)),
                ev_dates[ev], fat_tail_ts(rng, 300),
            ))
        copy_in(self.cur, "analytics_eventanalytics",
                COPY_TARGETS["analytics_eventanalytics"], ev_rows)

        i = 0
        for tt in self.ids["ticket_types"]:
            i += 1
            if rng.random() > 0.8:
                continue
            sold = rng.randint(0, 400)
            tt_rows.append((
                i, tt, sold, money(rng, 0, 80000),
                rng.choice([rng.randint(0, 300), -rng.randint(1, 40)]),
                fat_tail_ts(rng, 300), fat_tail_ts(rng, 400),
            ))
        copy_in(self.cur, "analytics_tickettypeanalytics",
                COPY_TARGETS["analytics_tickettypeanalytics"], tt_rows)
        return len(ev_rows) + len(tt_rows)

    def seed_notifications(self):
        n = self._n(N_NOTIFICATIONS, self.a.scale)
        rng = self.rng
        ev_names = {e: "Event" for e in self.ids["events"]}
        rows = []
        for i in range(1, n + 1):
            nt = rng.choice(NOTIF_TYPES)
            ev = rng.choice(self.ids["events"])
            msg = self.corrupt(NOTIF_MSG[nt].format(event=ev_names[ev]), 0.10)
            rows.append((
                i, rng.choice(self.ids["users"]), msg,
                fat_tail_ts(rng, 400), nt,
                rng.random() < 0.62,
                f"http://localhost:8000/events/{ev}/" if rng.random() < 0.7 else "",
            ))
        rows.sort(key=lambda r: r[3])
        copy_in(self.cur, "communication_notification",
                COPY_TARGETS["communication_notification"], rows)
        return len(rows)

    def seed_support_tickets(self):
        n = self._n(N_SUPPORT_TICKETS, self.a.scale)
        rng = self.rng
        rows = []
        for i in range(1, n + 1):
            subj = self.corrupt(rng.choice(SUPPORT_SUBJ), 0.12) or ""
            status = rng.choices(["Open", "In Progress", "Closed"],
                                 weights=[30, 25, 45])[0]
            closed = status == "Closed"
            created = fat_tail_ts(rng, 400)
            rows.append((
                i, rng.choice(self.ids["users"]), subj,
                f"{subj}. Please advise. " * rng.randint(1, 4),
                created, rng.choice(["L", "M", "M", "H", "", "urgent"]),
                status,
                rng.choice(self.ids["users"]) if rng.random() < 0.7 else None,
                # `resolution` is blank=True but NOT NULL in the DB -- another
                # spot where the model lies about nullability. Open tickets get
                # "" rather than NULL.
                f"Resolved in {rng.randint(1,72)}h" if closed else "",
                created + timedelta(hours=rng.randint(1, 500)) if closed else None,
            ))
        rows.sort(key=lambda r: r[4])
        copy_in(self.cur, "support_supportticket",
                COPY_TARGETS["support_supportticket"], rows)
        return len(rows)

    def seed_rsvp_history(self):
        rng = self.rng
        rows = []
        i = 0
        for rid, *_ in self.rsvp_meta:
            for _ in range(rng.randint(1, 3)):
                i += 1
                rows.append((i, rng.choice(self.ids["users"]), rid,
                             fat_tail_ts(rng, 500)))
        rows.sort(key=lambda r: r[3])
        copy_in(self.cur, "user_profiles_userrsvphistory",
                COPY_TARGETS["user_profiles_userrsvphistory"], rows)
        return len(rows)

    def seed_post_migration_fixes(self):
        """
        Content types are needed by django_admin_log; auth perms by the ORM.
        Both are normally created by migrations -- harmless if already present.
        """
        self.cur.execute("SELECT 1")
        return 0

    # -- driver -----------------------------------------------------------
    def run(self):
        stages = [
            ("roles", self.seed_roles),
            ("users", self.seed_users),
            ("user_roles", self.seed_user_roles),
            ("organizers", self.seed_organizers),
            ("events", self.seed_events),
            ("event_images", self.seed_event_images),
            ("ticket_types", self.seed_ticket_types),
            ("rsvps", self.seed_rsvps),
            ("transactions", self.seed_transactions),
            ("qrcodes", self.seed_qrcodes),
            ("event_bills", self.seed_event_bills),
            ("organizer_txns", self.seed_organizer_transactions),
            ("analytics", self.seed_analytics),
            ("notifications", self.seed_notifications),
            ("support_tickets", self.seed_support_tickets),
            ("rsvp_history", self.seed_rsvp_history),
        ]
        results = {}
        for name, fn in stages:
            n = fn()
            results[name] = n
            print(f"  {name:<20} {n:>9,}", flush=True)
        return results


# --------------------------------------------------------------------------
# Post-load mess injection (needs rows to exist)
# --------------------------------------------------------------------------
def resync_sequences(cur) -> list[str]:
    """
    COPY with an explicit `id` column does NOT advance the owning sequence, so
    after a bulk load every sequence still points at 1. Any later INSERT (or a
    second generator run without TRUNCATE ... RESTART IDENTITY) then collides.
    Reset each identity column to max(id).
    """
    # NB: for a serial column Postgres reports column_default as the bare
    # sequence name (e.g. 'public.payments_transaction_id_seq'), NOT as
    # nextval('...'). Matching on 'nextval%' silently finds nothing, so detect
    # identity columns by asking pg_get_serial_sequence instead.
    cur.execute("""
        SELECT c.table_name, c.column_name
          FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_name = c.table_name AND t.table_schema = c.table_schema
         WHERE c.table_schema = 'public'
           AND t.table_type = 'BASE TABLE'
           AND pg_get_serial_sequence(c.table_name, c.column_name) IS NOT NULL
    """)
    fixed = []
    for table, col in cur.fetchall():
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence(%s, %s), "
            f"COALESCE((SELECT MAX({col}) FROM {table}), 1))",
            (table, col),
        )
        fixed.append(f"{table}.{col}")
    return fixed


def inject_mess(cur, m: float, seed: int) -> dict[str, int]:
    """
    Mess that can only be expressed as SQL against existing rows:
    orphaned analytics, duplicated QR targets, cross-event billing,
    RSVPs pointing at users deleted long ago, and NULL-heavy columns.
    """
    rng = random.Random(seed + 7)
    out = {}
    if m <= 0:
        return out

    # `cur` is already an open psycopg cursor.
    c = cur

    # Defects are injected with TABLESAMPLE SYSTEM (n) rather than `id % N`:
    # the modulus trick matched almost nothing at small scale and made the
    # audit numbers meaningless as a fraction of the table.
    # 1. RSVPs whose attendee no longer exists -> would violate the FK,
    #    so instead we blank out nullable descriptive fields and leave
    #    referential orphans in tables where the FK allows SET NULL.
    c.execute("""
        UPDATE events_rsvp
           SET metadata = NULL, transaction_id = ''
         WHERE id IN (SELECT id FROM events_rsvp TABLESAMPLE SYSTEM (3))
    """)
    out["rsvp_nulled"] = c.rowcount

    # 2. Analytics rows for events that no longer exist (OneToOne PROTECT
    #    means we must delete the event, not the analytics -- simulate a
    #    botched cleanup by pointing some analytics at wrong events).
    c.execute("""
        UPDATE analytics_eventanalytics a
           SET total_revenue = 0, total_rsvps = 0
         WHERE a.id IN (SELECT id FROM analytics_eventanalytics TABLESAMPLE SYSTEM (25))
    """)
    out["analytics_zeroed"] = c.rowcount

    # 3. Duplicate logical records: copy some transactions with new ids
    #    so (user, ticket_type, amount) repeats -- great for testing
    #    DISTINCT / GROUP BY assumptions.
    c.execute("""
        -- id omitted on purpose: let the sequence assign fresh ids so the
        -- copies don't collide with the originals.
        INSERT INTO payments_transaction
            (user_id, rsvp_id, ticket_type_id, currency, amount, quantity,
             payment_method, transaction_id, session_id, payment_status,
             billing_info, created_at)
        SELECT user_id, NULL, ticket_type_id, currency, amount, quantity,
               payment_method, transaction_id, session_id, payment_status,
               billing_info, created_at
          FROM payments_transaction
         WHERE id IN (SELECT id FROM payments_transaction TABLESAMPLE SYSTEM (4))
    """)
    out["dup_transactions"] = c.rowcount

    # 4. Amounts that disagree with the RSVP's total_charge (money bugs).
    c.execute("""
        UPDATE payments_transaction t
           SET amount = (
                   SELECT r.total_charge * 1.5
                     FROM events_rsvp r
                    WHERE r.id = t.rsvp_id
               )
         WHERE t.rsvp_id IS NOT NULL
           AND t.id IN (SELECT id FROM payments_transaction TABLESAMPLE SYSTEM (6))
    """)
    out["amount_mismatches"] = c.rowcount

    # 5. Bills whose amount has nothing to do with their event's revenue.
    c.execute("""
        UPDATE payments_eventpaymentbill
           SET amount = 0
         WHERE id IN (SELECT id FROM payments_eventpaymentbill TABLESAMPLE SYSTEM (20))
    """)
    out["zero_bills"] = c.rowcount

    # 6. Notifications pointing at URLs for events that don't exist.
    c.execute("""
        UPDATE communication_notification
           SET link = 'http://localhost:8000/events/99999999/'
         WHERE id IN (SELECT id FROM communication_notification TABLESAMPLE SYSTEM (3))
    """)
    out["dead_links"] = c.rowcount

    # 7. Closed support tickets with no resolution, and resolved_at
    #    timestamps that precede created_at.
    c.execute("""
        UPDATE support_supportticket
           SET resolution = '', resolved_at = NULL
         WHERE status = 'Closed'
           AND id IN (SELECT id FROM support_supportticket TABLESAMPLE SYSTEM (30))
    """)
    out["tickets_unresolved"] = c.rowcount
    c.execute("""
        UPDATE support_supportticket
           SET resolved_at = created_at - INTERVAL '3 days'
         WHERE resolved_at IS NOT NULL
           AND id IN (SELECT id FROM support_supportticket TABLESAMPLE SYSTEM (15))
    """)
    out["tickets_backdated"] = c.rowcount

    # 8. Ticket oversell.
    #    NOT possible here: TicketType.quantity_available is a PositiveIntegerField,
    #    so Postgres enforces CHECK (quantity_available >= 0). Drive it to 0
    #    instead -- "sold out with inventory left" is the realistic defect and
    #    the only one this schema will actually allow.
    c.execute("""
        UPDATE events_tickettype
           SET quantity_available = 0
         WHERE id IN (SELECT id FROM events_tickettype TABLESAMPLE SYSTEM (7))
    """)
    out["sold_out"] = c.rowcount

    # 9. Events marked finished long before their date, and published
    #    events in the future with is_active = false.
    c.execute("""
        UPDATE events_event
           SET has_finished_event = TRUE
         WHERE date > NOW()
           AND id IN (SELECT id FROM events_event TABLESAMPLE SYSTEM (40))
    """)
    out["future_finished"] = c.rowcount

    # 10. Whitespace/unicode in emails would normally be impossible; the
    #     email column is unique but not format-checked at DB level.
    c.execute("""
        UPDATE user_profiles_customuser
           SET first_name = btrim(first_name)
         WHERE first_name LIKE ' %'
    """)
    out["trimmed_names"] = c.rowcount

    return out


# --------------------------------------------------------------------------
# Diagnostics: prove the mess is actually there
# --------------------------------------------------------------------------
DIAGNOSTICS = [
    ("users w/ padded or uppercase names",
     "SELECT count(*) FROM user_profiles_customuser WHERE first_name <> btrim(first_name) OR first_name <> lower(first_name)"),
    ("users w/ blank phone/address",
     "SELECT count(*) FROM user_profiles_customuser WHERE phone = '' OR address = ''"),
    ("duplicate organizer slugs",
     "SELECT count(*) FROM (SELECT organizer_slug FROM user_profiles_organizer WHERE organizer_slug <> '' GROUP BY 1 HAVING count(*) > 1) x"),
    ("events w/ NULL description_html",
     "SELECT count(*) FROM events_event WHERE description_html IS NULL"),
    ("events w/ NULL metadata",
     "SELECT count(*) FROM events_event WHERE metadata IS NULL"),
    ("ticket types priced <= 0",
     "SELECT count(*) FROM events_tickettype WHERE price <= 0"),
    ("ticket types marked sold out (qty = 0)",
     "SELECT count(*) FROM events_tickettype WHERE quantity_available = 0"),
    ("ticket types w/ zero qty but real RSVPs (oversold in reality)",
     """SELECT count(*) FROM events_tickettype t
          WHERE t.quantity_available = 0
            AND EXISTS (SELECT 1 FROM events_rsvp r
                         JOIN payments_transaction p ON p.ticket_type_id = t.id
                        WHERE p.rsvp_id = r.id AND r.ticket_qty > 0)"""),
    ("RSVPs cancelled AND attended (impossible)",
     "SELECT count(*) FROM events_rsvp WHERE is_cancelled AND is_attended"),
    ("RSVPs w/ NULL metadata",
     "SELECT count(*) FROM events_rsvp WHERE metadata IS NULL"),
    ("transactions w/ NULL transaction_id",
     "SELECT count(*) FROM payments_transaction WHERE transaction_id IS NULL"),
    ("duplicate transaction_id values",
     "SELECT count(*) FROM (SELECT transaction_id FROM payments_transaction WHERE transaction_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1) x"),
    ("transactions w/ negative amount",
     "SELECT count(*) FROM payments_transaction WHERE amount < 0"),
    ("txn amount <> rsvp total_charge",
     "SELECT count(*) FROM payments_transaction t JOIN events_rsvp r ON r.id = t.rsvp_id WHERE t.amount <> r.total_charge"),
    ("transactions w/ no RSVP",
     "SELECT count(*) FROM payments_transaction WHERE rsvp_id IS NULL"),
    ("analytics rows disagreeing with truth",
     """SELECT count(*) FROM analytics_eventanalytics a
          WHERE a.total_rsvps <> (SELECT count(*) FROM events_rsvp r WHERE r.event_id = a.event_id)"""),
    ("events w/o analytics row",
     "SELECT count(*) FROM events_event e WHERE NOT EXISTS (SELECT 1 FROM analytics_eventanalytics a WHERE a.event_id = e.id)"),
    # `resolution` is NOT NULL in the DB, so "no resolution" is '' not NULL.
    ("support tickets closed w/ empty resolution",
     "SELECT count(*) FROM support_supportticket WHERE status = 'Closed' AND resolution = ''"),
    ("support tickets still Open (unresolved backlog)",
     "SELECT count(*) FROM support_supportticket WHERE status = 'Open'"),
    ("support tickets resolved before created",
     "SELECT count(*) FROM support_supportticket WHERE resolved_at < created_at"),
    ("notifications w/ empty link",
     "SELECT count(*) FROM communication_notification WHERE link = ''"),
    ("future events marked finished",
     "SELECT count(*) FROM events_event WHERE date > NOW() AND has_finished_event"),
]


def print_diagnostics(cur) -> None:
    print("\n=== data quality audit (these are INTENTIONAL defects) ===")
    for label, sql in DIAGNOSTICS:
        cur.execute(sql)
        print(f"  {label:<45} {cur.fetchone()[0]:>9,}")


def table_sizes(cur) -> None:
    cur.execute("""
        SELECT relname, n_live_tup
          FROM pg_stat_user_tables
         ORDER BY n_live_tup DESC
    """)
    rows = cur.fetchall()
    print("\n=== table sizes ===")
    total = 0
    for name, n in rows:
        print(f"  {name:<42} {n:>10,}")
        total += n or 0
    print(f"  {'TOTAL (approx)':<42} {total:>10,}")


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", required=True,
                   help="postgresql://user@host:port/dbname")
    p.add_argument("--scale", type=float, default=1.0,
                   help="volume multiplier (1.0 ~= 300K rows)")
    p.add_argument("--mess", type=float, default=1.0,
                   help="messiness 0.0 (clean) .. 1.0 (full chaos)")
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--truncate", action="store_true",
                   help="TRUNCATE all app tables first")
    p.add_argument("--audit", action="store_true", default=True,
                   help="run the data-quality audit afterwards")
    p.add_argument("--no-audit", dest="audit", action="store_false")
    args = p.parse_args()

    if not 0.0 <= args.mess <= 1.0:
        p.error("--mess must be between 0.0 and 1.0")

    print(f"connecting: {args.dsn.split('@')[-1]}")
    with psycopg.connect(args.dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            if args.truncate:
                print("truncating app tables ...")
                cur.execute("""
                    DO $$
                    DECLARE t text;
                    BEGIN
                      FOR t IN
                        SELECT tablename FROM pg_tables
                         WHERE schemaname = 'public'
                           AND tablename NOT IN (
                                'django_migrations', 'django_content_type',
                                'auth_permission', 'django_session',
                                'django_admin_log'
                           )
                      LOOP
                        EXECUTE format('TRUNCATE TABLE %I RESTART IDENTITY CASCADE', t);
                      END LOOP;
                    END $$;
                """)
            cur.execute("SET synchronous_commit = off")
            load_column_limits(cur)

            print(f"generating (scale={args.scale}, mess={args.mess}) ...")
            g = Generator(cur, args)
            results = g.run()

            print("resyncing sequences ...")
            seqs = resync_sequences(cur)
            results["_sequences_resynced"] = len(seqs)

            print("applying post-load mess ...")
            mess = inject_mess(cur, args.mess, args.seed)

        conn.commit()

        # NOTE: the `with conn.cursor()` block above has exited, which closed
        # that cursor. Reporting needs a fresh one.
        with conn.cursor() as rcur:
            rcur.execute("ANALYZE")
            conn.commit()

            print("\n=== rows written ===")
            for k, v in results.items():
                print(f"  {k:<24} {v:>9,}")
            for k, v in mess.items():
                print(f"  ~{k:<23} {v:>9,}")

            table_sizes(rcur)
            if args.audit:
                print_diagnostics(rcur)

    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
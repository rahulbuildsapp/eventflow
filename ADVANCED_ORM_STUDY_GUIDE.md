# EventFlow Advanced ORM Study Guide

## Project Business Logic Summary

### Core Concept
EventFlow is an event management platform where:
- **Organizers** (users with organizer role) create and manage events
- **Attendees** RSVP to events and purchase tickets
- **Payments** are processed via Stripe
- **QR codes** are generated for ticket validation
- **Analytics** track event performance
- **Notifications** keep users informed

### Key Workflows
1. **Event Creation**: Organizer → Create Event → Add Ticket Types → Publish
2. **RSVP Flow**: Attendee → RSVP → Select Tickets → Stripe Checkout → Payment → QR Code
3. **Attendance**: QR Scan → Validate → Mark Attended
4. **Analytics**: Periodic tasks update stats (revenue, attendance, etc.)

---

## Advanced Query Patterns - Theory & Practice

### 1. select_related() - Foreign Key Optimization

**Theory**: Uses SQL JOIN to fetch related objects in one query. Use for ForeignKey and OneToOne relationships.

**When to think**: "I need data from a related object that has a direct FK to this model"

**EventFlow Example** (`events/views.py:234`):
```python
Event.objects.select_related('organizer').get(id=event_id)
# Without this: 2 queries (event + organizer)
# With this: 1 query with JOIN
```

**Practice Questions**:

**Q1.** You need to display RSVP details including: attendee name, event name, ticket type name, and payment status. Write a single query.

<details>
<summary>Solution approach</summary>

```python
RSVP.objects.filter(
    event_id=some_event_id
).select_related(
    'attendee',           # FK to CustomUser
    'event',              # FK to Event
    'transaction__ticket_type',  # FK to Transaction → FK to TicketType
    'transaction'          # FK to Transaction for payment_status
)
```
Think: Follow each FK relationship with double underscores for nested relationships.

</details>

**Q2.** In `payments/tasks.py:30`, why do we use `select_related('ticket_type__event', 'user', 'rsvp')` instead of just `'ticket_type'`?

<details>
<summary>Answer</summary>

We need `ticket_type.event` and `rsvp` data later in the function. By doing `ticket_type__event`, we also join the Event table in the same query, avoiding an extra query when accessing `ticket_type.event`.

</details>

---

### 2. prefetch_related() - Reverse FK & Many-to-Many

**Theory**: Executes separate queries and joins in Python. Use for reverse ForeignKey, ManyToMany relationships.

**When to think**: "I need to access a set of related objects (reverse FK) or M2M - the 'many' side"

**EventFlow Example** (`events/api/views.py:36`):
```python
Event.objects.select_related('organizer').prefetch_related(
    'images',           # Reverse FK from EventImage
    'tickettype_set',   # Reverse FK from TicketType
    'attendees'         # Reverse FK from RSVP
)
```

**Practice Questions**:

**Q3.** You're displaying an event page that shows:
- Event details (organizer info)
- All ticket types with remaining quantity
- All images
- Total RSVP count

Write the optimized query and explain why each optimization is needed.

<details>
<summary>Solution</summary>

```python
event = Event.objects.select_related('organizer').prefetch_related(
    'images',
    'tickettype_set'
).get(id=event_id)

# For RSVP count, either:
# Option 1: annotate during query
from django.db.models import Count
event = Event.objects.select_related('organizer').prefetch_related(
    'images', 'tickettype_set'
).annotate(rsvp_count=Count('attendees')).get(id=event_id)

# Option 2: separate aggregation if you have many events
rsvp_count = RSVP.objects.filter(event=event).count()
```

`select_related('organizer')`: Single event → need organizer → FK relationship
`prefetch_related('images', 'tickettype_set')`: Multiple images/types per event → reverse FK

</details>

**Q4.** What happens if you use `select_related()` instead of `prefetch_related()` for `tickettype_set`?

<details>
<summary>Answer</summary>

Django will raise an error or warning. `select_related` uses SQL JOIN which doesn't work efficiently for reverse FK relationships where there could be multiple related objects. `prefetch_related` does a separate query and stitches results in Python.

</details>

---

### 3. Q() Objects - Complex Query Logic

**Theory**: Allows OR conditions and complex combinations that `__or` can't handle well.

**When to think**: "I need to filter by A OR B" or "complex AND/OR combinations"

**EventFlow Example** (`events/views.py:900-903`):
```python
events = events.filter(
    Q(name__icontains=search_query) |
    Q(description__icontains=search_query) |
    Q(location__icontains=search_query)
)
```

**Practice Questions**:

**Q5.** Find all events where:
- (status is published AND date is in future) OR (organizer is a specific user AND status is active)

Write the Q object query.

<details>
<summary>Solution</summary>

```python
from django.db.models import Q
from datetime import datetime

events = Event.objects.filter(
    Q(is_published=True, date__gt=datetime.now()) |
    Q(organizer__user=specific_user, is_active=True)
)
```

Note: Commas inside Q() act as AND. The `|` operator means OR.

</details>

**Q6.** Find RSVPs that are either:
- Completed but not attended, OR
- Cancelled but payment was made (payment_status='PAID')

<details>
<summary>Solution</summary>

```python
from django.db.models import Q

rsvps = RSVP.objects.filter(
    Q(is_completed=True, is_attended=False) |
    Q(is_cancelled=True, transaction__payment_status='PAID')
).select_related('transaction')
```

</details>

---

### 4. F() Expressions - Database-Level References

**Theory**: References a field value at the database level, not in Python memory.

**When to think**: "I need to compare/update a field with another field" or "avoid race conditions in increments"

**EventFlow Example** (`events/views.py:987-988`):
```python
ticket_types = rsvps.filter(...).values(
    ticket_id=F('transaction__ticket_type__id'),
    ticket_name=F('transaction__ticket_type__name')
).distinct()
```

**Practice Questions**:

**Q7.** You need to find all events where tickets sold (from RSVP count) exceeds 80% of total capacity. Write the query using F() and annotate.

<details>
<summary>Solution</summary>

```python
from django.db.models import F, Count, Sum, Case, When, Value, BooleanField

events = Event.objects.annotate(
    total_capacity=Sum('tickettype_set__quantity_available'),
    tickets_sold=Count('attendees', filter=Q(attendees__is_completed=True))
).filter(
    tickets_sold__gt=F('total_capacity') * 0.8
)
```

</details>

**Q8.** Increment a TicketType's `quantity_sold` by the RSVP's `ticket_qty` atomically.

<details>
<summary>Solution</summary>

```python
from django.db.models import F

TicketType.objects.filter(id=ticket_type_id).update(
    quantity_sold=F('quantity_sold') + ticket_qty
)
```

This happens at DB level - no race condition between read and write.

</details>

---

### 5. Subquery() and OuterRef() - Advanced Nested Queries

**Theory**: Subquery allows a queryset to be used as a value in another queryset. OuterRef references the "outer" query's field.

**When to think**: "I need to annotate each row with an aggregated value from a related table"

**EventFlow Example** (`events/views.py:707-735`):
```python
from django.db.models import Subquery, OuterRef, Sum, Count

# Step 1: Define subquery
ticket_capacity_subquery = TicketType.objects.filter(
    event_id=OuterRef('id')  # References Event.id in outer query
).values('event_id').annotate(
    total_capacity=Sum('quantity_available')
).values('total_capacity')[:1]  # Returns a single value

# Step 2: Use in annotation
events = Event.objects.annotate(
    capacity=Subquery(ticket_capacity_subquery)
)
```

**Practice Questions**:

**Q9.** Annotate each Event with:
- Total revenue from completed RSVPs
- Count of attended RSVPs
- Name of the first ticket type

Write the query using Subquery.

<details>
<summary>Solution</summary>

```python
from django.db.models import Subquery, OuterRef, Sum, Count

# Revenue subquery
revenue_subq = RSVP.objects.filter(
    event_id=OuterRef('id'),
    is_completed=True
).values('event_id').annotate(
    total=Sum('total_charge')
).values('total')[:1]

# Attended count subquery
attended_subq = RSVP.objects.filter(
    event_id=OuterRef('id'),
    is_attended=True
).values('event_id').annotate(
    count=Count('id')
).values('count')[:1]

# First ticket type name
first_ticket_subq = TicketType.objects.filter(
    event_id=OuterRef('id')
).order_by('created_at').values('name')[:1]

events = Event.objects.annotate(
    total_revenue=Subquery(revenue_subq),
    attended_count=Subquery(attended_subq),
    first_ticket_name=Subquery(first_ticket_subq)
)
```

**Thinking process**: Each Subquery:
1. Filters by the outer query's ID (OuterRef)
2. Groups/aggregates if needed
3. Returns a single value with `[:1]`
4. Gets annotated to each row

</details>

**Q10.** (Challenge) For a dashboard, get the top 5 events by revenue, showing organizer name and revenue, in one query.

<details>
<summary>Solution</summary>

```python
from django.db.models import Subquery, OuterRef, Sum

revenue_subq = RSVP.objects.filter(
    event_id=OuterRef('id'),
    is_completed=True
).values('event_id').annotate(
    revenue=Sum('total_charge')
).values('revenue')[:1]

top_events = Event.objects.select_related('organizer').annotate(
    revenue=Subquery(revenue_subq)
).order_by('-revenue')[:5]
```

</details>

---

### 6. annotate() vs aggregate()

**Theory**:
- `annotate()`: Adds a field to EACH row (per-group)
- `aggregate()`: Returns a single dictionary with calculated values

**EventFlow Example** (`events/views.py:49-52`):
```python
# aggregate - single result
stats = RSVP.objects.filter(...).aggregate(
    registered=Count('id'),
    revenue=Sum('total_charge')
)
# Returns: {'registered': 10, 'revenue': Decimal('500.00')}

# annotate - per-row
events = Event.objects.annotate(
    rsvp_count=Count('attendees')
)
# Each event now has rsvp_count attribute
```

**Practice Questions**:

**Q11.** Get a list of organizers with their total events count and total revenue across all their events.

<details>
<summary>Solution</summary>

```python
from django.db.models import Count, Sum, Subquery, OuterRef

# Method 1: Using annotate on Organizer
from events.models import Event, RSVP

organizers = Organizer.objects.annotate(
    event_count=Count('event_set'),
    total_revenue=Sum('event_set__attendees__total_charge', 
                      filter=Q(event_set__attendees__is_completed=True))
)

# Method 2: Using Subquery (more explicit)
revenue_subq = Event.objects.filter(
    organizer_id=OuterRef('id')
).values('organizer_id').annotate(
    total=Sum('attendees__total_charge', 
              filter=Q(attendees__is_completed=True))
).values('total')[:1]

organizers = Organizer.objects.annotate(
    event_count=Count('event_set'),
    total_revenue=Subquery(revenue_subq)
)
```

</details>

---

### 7. select_for_update() - Race Condition Protection

**Theory**: Locks the database row so no other transaction can modify it until the current transaction completes.

**When to think**: "Multiple processes might update the same row simultaneously" (e.g., ticket sales)

**EventFlow Example** (`events/models.py:154-163`):
```python
from django.db import transaction

if self.is_completed and not self.pk:
    with transaction.atomic():
        # Lock the ticket type row
        ticket_type = self.event.tickettype_set.select_for_update().first()
        if ticket_type.quantity_available >= self.ticket_qty:
            ticket_type.quantity_available -= self.ticket_qty
            ticket_type.save(update_fields=['quantity_available'])
        else:
            raise ValueError("Not enough tickets")
```

**Practice Questions**:

**Q12.** You're building a coupon system where each coupon has `uses_remaining`. Multiple users might apply the same coupon simultaneously. Write the code to safely decrement uses.

<details>
<summary>Solution</summary>

```python
from django.db import transaction
from .models import Coupon

def apply_coupon(coupon_id):
    with transaction.atomic():
        coupon = Coupon.objects.select_for_update().get(id=coupon_id)
        if coupon.uses_remaining > 0:
            coupon.uses_remaining -= 1
            coupon.save(update_fields=['uses_remaining'])
            return True
        return False
```

</details>

---

### 8. TruncMonth, TruncDay - Time-based Grouping

**Theory**: Database-level truncation for time-based aggregations.

**EventFlow Example** (`events/views.py:758-763`):
```python
from django.db.models.functions import TruncMonth

rsvp_by_month = RSVP.objects.filter(
    event__organizer=organizer
).annotate(
    month=TruncMonth('created_at')
).values('month').annotate(
    attendees=Count('id'),
    revenue=Sum('total_charge')
).order_by('month')
```

**Practice Questions**:

**Q13.** Get daily ticket sales for an event for the last 7 days.

<details>
<summary>Solution</summary>

```python
from django.db.models.functions import TruncDay
from django.db.models import Count, Sum
from datetime import datetime, timedelta

seven_days_ago = datetime.now() - timedelta(days=7)

daily_sales = RSVP.objects.filter(
    event=event,
    is_completed=True,
    created_at__gte=seven_days_ago
).annotate(
    day=TruncDay('created_at')
).values('day').annotate(
    tickets_sold=Count('id'),
    revenue=Sum('total_charge')
).order_by('day')
```

</details>

---

### 9. JSONField Queries

**Theory**: PostgreSQL and SQLite JSONField support querying inside JSON data.

**EventFlow Example** (`events/views.py:795-796`):
```python
event_categories = organized_events.values(
    'metadata__category'  # Double underscore to access JSON key
).annotate(
    count=Count('id')
).order_by('-count')
```

**Practice Questions**:

**Q14.** Find all events where metadata contains `{'is_virtual': True}` and the category is 'Tech'.

<details>
<summary>Solution</summary>

```python
# For PostgreSQL
events = Event.objects.filter(
    metadata__is_virtual=True,
    metadata__category='Tech'
)

# For SQLite (different syntax)
events = Event.objects.filter(
    metadata__contains={'is_virtual': True},
    metadata__category='Tech'
)
```

</details>

---

## Optimization Thinking Patterns

### Pattern 1: The "N+1 Problem" Detection
**Think**: "Am I accessing related data inside a loop?"

```python
# BAD - N+1 queries
events = Event.objects.all()
for event in events:
    print(event.organizer.name)  # Hits DB for each event

# GOOD - select_related
events = Event.objects.select_related('organizer').all()
for event in events:
    print(event.organizer.name)  # No extra DB hits
```

### Pattern 2: The "Bulk Annotation" Technique
**EventFlow Example** (`events/views.py:918-926`):
```python
# Instead of annotating each event individually (N queries)
# Do one query to get all stats for events on current page
event_ids = [e.id for e in events_page]  # From paginator
stats = RSVP.objects.filter(
    event_id__in=event_ids,
    is_cancelled=False
).values('event_id').annotate(
    registered_count=Count('id'),
    attended_count=Count('id', filter=Q(is_attended=True))
)
# Then map stats back to events in Python
```

### Pattern 3: The "Subquery vs Prefetch" Decision
- **Use Subquery**: When you need an aggregated value (count, sum) per row
- **Use prefetch_related**: When you need the actual related objects

---

## Practice Challenges

### Challenge 1: Event Dashboard Query
Create a single query for an organizer dashboard showing:
- Event name, date, capacity
- Tickets sold (completed RSVPs)
- Revenue
- Attendance rate (attended / sold)

### Challenge 2: Popular Events
Find events where:
- More than 50% of tickets are sold
- At least 10 RSVPs
- Event is in the future
Order by attendance rate.

### Challenge 3: Organizer Leaderboard
Rank organizers by:
- Total revenue across all their events
- Total attendees
- Average rating (if you had a rating field)

---

## How to Practice

1. **Read the code**: Look at the specific line numbers mentioned
2. **Try the practice questions**: Write your solution BEFORE looking at the answer
3. **Explain aloud**: Can you explain WHY a certain optimization was chosen?
4. **Modify scenarios**: "What if I needed X instead of Y? How would the query change?"
5. **Benchmark**: Use `django.db.connection.queries` to see actual SQL generated

```python
from django.db import connection
# After your query
print(connection.queries)  # Shows all SQL executed
```

---

## Key Mental Models

1. **select_related = JOIN** (FK, OneToOne, "single" side)
2. **prefetch_related = Separate query + Python join** (reverse FK, M2M, "many" side)
3. **Subquery = SQL subquery** (when you need aggregated values per row)
4. **F() = Database field reference** (compare fields, atomic updates)
5. **Q() = Complex conditions** (OR logic, combine filters)
6. **annotate = Add column to results** (per-row calculation)
7. **aggregate = Collapse to single result** (summary stats)

---

## Next Steps

1. Read through each practice question
2. Write your solution in a Django shell or test file
3. Compare with the provided solution
4. Come back with your solutions and I'll review them
5. Ask "What if..." scenarios to deepen understanding

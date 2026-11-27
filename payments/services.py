import uuid
from decimal import Decimal

from django.db import transaction
from events.models import Event, RSVP, TicketType
from qr_codes.models import QRCode

from .models import Transaction

FREE_TICKET_NAME = "Free RSVP"
FREE_TICKET_INVENTORY = 1_000_000


def get_or_create_free_ticket(event):
    """Return a zero-price ticket for events that do not sell tickets."""
    ticket = TicketType.objects.filter(
        event=event, price=0, is_system_generated=True
    ).order_by("id").first()
    if ticket:
        return ticket

    return TicketType.objects.create(
        event=event,
        name=FREE_TICKET_NAME,
        price=Decimal("0.00"),
        currency="INR",
        quantity_available=FREE_TICKET_INVENTORY,
        stripe_price_id=None,
        is_system_generated=True,
    )


def create_free_rsvp(user, event, ticket_type=None, quantity=1, billing_info=None):
    """
    Create a confirmed RSVP + zero-value transaction + QR ticket without Stripe.

    Returns (rsvp, created). Existing active RSVPs for the same ticket type are
    returned instead of creating duplicates.
    """
    quantity = int(quantity or 1)
    if quantity < 1:
        raise ValueError("Quantity must be at least 1.")

    if event.has_finished_event:
        raise ValueError("This event has already finished.")
    if not event.is_published or not event.is_active:
        raise ValueError("This event is not open for registration.")

    with transaction.atomic():
        if ticket_type is None:
            ticket_type = get_or_create_free_ticket(event)

        if ticket_type.event_id != event.id:
            raise ValueError("This ticket does not belong to the event.")

        ticket_type = TicketType.objects.select_for_update().get(pk=ticket_type.pk)
        if ticket_type.price != 0:
            raise ValueError("This ticket requires payment.")
        if ticket_type.quantity_available < quantity:
            raise ValueError(
                f"Not enough tickets available for {ticket_type.name}. "
                f"Only {ticket_type.quantity_available} left."
            )

        existing = (
            RSVP.objects.filter(
                event=event,
                attendee=user,
                is_cancelled=False,
                transaction__ticket_type=ticket_type,
            )
            .select_related("event")
            .first()
        )
        if existing:
            return existing, False

        ticket_type.quantity_available -= quantity
        ticket_type.save(update_fields=["quantity_available"])

        reference = f"free_{uuid.uuid4().hex[:20]}"
        txn = Transaction.objects.create(
            user=user,
            ticket_type=ticket_type,
            currency=ticket_type.currency or "INR",
            amount=Decimal("0.00"),
            quantity=quantity,
            transaction_id=reference,
            session_id=reference,
            payment_status="SUCCESS",
            billing_info=billing_info,
        )

        rsvp = RSVP.objects.create(
            event=event,
            attendee=user,
            transaction_id=reference,
            ticket_qty=quantity,
            total_charge=Decimal("0.00"),
            is_active=True,
            metadata={"free": True},
        )

        txn.rsvp = rsvp
        txn.save(update_fields=["rsvp"])

        # Generate synchronously so the attendee can view/download the ticket
        # immediately even when no Celery worker is running.
        QRCode.objects.get_or_create(transaction=txn)

        return rsvp, True
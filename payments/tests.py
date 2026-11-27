import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from events.models import Event, TicketType, RSVP
from payments.models import Transaction
from payments.tasks import process_checkout_session
from user_profiles.models import Organizer, Role
from unittest.mock import patch

User = get_user_model()


@pytest.fixture
def buyer_user(db):
    role, _ = Role.objects.get_or_create(name='attendee')
    user = User.objects.create_user(
        username='buyer_user',
        email='buyer@example.com',
        password='testpassword',
        current_role=role
    )
    user.available_roles.add(role)
    return user


@pytest.fixture
def ticket_event(db):
    role, _ = Role.objects.get_or_create(name='organizer')
    org_user = User.objects.create_user(
        username='event_host',
        email='host@example.com',
        password='testpassword',
        current_role=role
    )
    org_user.available_roles.add(role)
    organizer = Organizer.objects.create(
        user=org_user,
        organizer_name='Host Org',
        organizer_email='host@example.com',
        created_by=org_user,
        is_active=True
    )
    event = Event.objects.create(
        organizer=organizer,
        name='Exclusive Concert',
        description='Only limited tickets',
        date=timezone.now() + timezone.timedelta(days=5),
        location='Mumbai',
        is_published=True,
        is_active=True
    )
    ticket = TicketType.objects.create(
        event=event,
        name='Pass',
        price=100.00,
        quantity_available=5,
        stripe_price_id='price_pass_123'
    )
    return ticket


@pytest.mark.django_db
def test_pre_checkout_rejects_insufficient_stock(api_client, buyer_user, ticket_event):
    api_client.force_authenticate(user=buyer_user)
    url = reverse('ticket_payment')
    # Try requesting 10 tickets when only 5 are available
    payload = {
        'tickets': [{'ticket_id': ticket_event.id, 'quantity': 10}]
    }
    response = api_client.post(url, data=payload, format='json')
    assert response.status_code == 400
    assert 'Not enough tickets available' in response.data['error']


@pytest.mark.django_db
@patch('qr_codes.tasks.generate_qr_code.delay')
@patch('communication.tasks.mail_tasks.ticket_payment_user_mail.delay')
def test_process_checkout_session_atomically_decrements_inventory(
    mock_mail, mock_qr, buyer_user, ticket_event
):
    session_id = 'cs_test_session_abc'
    initial_qty = ticket_event.quantity_available
    purchased_qty = 2

    txn = Transaction.objects.create(
        user=buyer_user,
        ticket_type=ticket_event,
        currency='INR',
        amount=ticket_event.price,
        quantity=purchased_qty,
        payment_status='PENDING',
        session_id=session_id,
        transaction_id='pi_test_123'
    )

    result = process_checkout_session(session_id)
    assert result['status'] == 'success'
    assert result['processed'] == 1

    # Verify inventory was decremented accurately
    ticket_event.refresh_from_db()
    assert ticket_event.quantity_available == initial_qty - purchased_qty

    # Verify transaction and RSVP status
    txn.refresh_from_db()
    assert txn.payment_status == 'SUCCESS'
    assert txn.rsvp is not None
    assert txn.rsvp.is_active is True
    assert txn.rsvp.ticket_qty == purchased_qty


@pytest.mark.django_db
@patch('qr_codes.tasks.generate_qr_code.delay')
@patch('communication.tasks.mail_tasks.ticket_payment_user_mail.delay')
def test_process_checkout_session_fails_when_oversold(
    mock_mail, mock_qr, buyer_user, ticket_event
):
    session_id = 'cs_test_oversold'
    # Set available to 1, but transaction demands 2
    ticket_event.quantity_available = 1
    ticket_event.save()

    txn = Transaction.objects.create(
        user=buyer_user,
        ticket_type=ticket_event,
        currency='INR',
        amount=ticket_event.price,
        quantity=2,
        payment_status='PENDING',
        session_id=session_id,
        transaction_id='pi_test_oversold'
    )

    with pytest.raises(ValueError, match="Not enough tickets available"):
        process_checkout_session(session_id)

    # Quantity should remain untouched due to transaction atomic rollback
    ticket_event.refresh_from_db()
    assert ticket_event.quantity_available == 1


@pytest.fixture
def free_event(db):
    role, _ = Role.objects.get_or_create(name='organizer')
    host = User.objects.create_user(
        username='free_event_host',
        email='freehost@example.com',
        password='testpassword',
        current_role=role
    )
    host.available_roles.add(role)
    organizer = Organizer.objects.create(
        user=host,
        organizer_name='Free Events Org',
        organizer_email='freehost@example.com',
        created_by=host,
        is_active=True
    )
    return Event.objects.create(
        organizer=organizer,
        name='Free Community Meetup',
        description='No paid tickets',
        date=timezone.now() + timezone.timedelta(days=7),
        location='Pune',
        is_published=True,
        is_active=True
    )


@pytest.mark.django_db
def test_free_checkout_creates_ticket_without_stripe(api_client, buyer_user, free_event):
    from qr_codes.models import QRCode

    ticket = TicketType.objects.create(
        event=free_event,
        name='Free Pass',
        price=0,
        currency='INR',
        quantity_available=10
    )
    api_client.force_authenticate(user=buyer_user)

    response = api_client.post(
        reverse('free-checkout'),
        data={'tickets': [{'ticket_id': ticket.id, 'quantity': 2}]},
        format='json'
    )

    assert response.status_code == 200
    assert response.data['success'] is True
    assert response.data['redirect_url'].startswith('/ticket/')

    ticket.refresh_from_db()
    assert ticket.quantity_available == 8

    rsvp = RSVP.objects.get(event=free_event, attendee=buyer_user)
    assert rsvp.ticket_qty == 2
    assert rsvp.total_charge == 0
    assert Transaction.objects.filter(rsvp=rsvp, payment_status='SUCCESS').exists()
    assert QRCode.objects.filter(transaction__rsvp=rsvp).exists()


@pytest.mark.django_db
def test_free_rsvp_for_event_without_tickets(client, buyer_user, free_event):
    client.force_login(buyer_user)

    response = client.post(reverse('free-rsvp', args=[free_event.id]))

    assert response.status_code == 200
    payload = response.json()
    assert payload['success'] is True
    assert payload['redirect_url'] == reverse('preview-ticket', args=[payload['ticket_id']])
    assert TicketType.objects.filter(event=free_event, name='Free RSVP').exists()
    assert RSVP.objects.filter(event=free_event, attendee=buyer_user).exists()

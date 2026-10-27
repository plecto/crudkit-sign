"""Periodic work. Schedule ``expire_requests`` hourly and ``send_reminders`` daily with Celery beat."""

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from crudkit_sign.conf import get_setting
from crudkit_sign.models import SignatureRequest, Signer

OPEN = (SignatureRequest.Status.SENT, SignatureRequest.Status.PARTIALLY_SIGNED)


@shared_task
def expire_requests():
    expired = 0
    for request in SignatureRequest.objects.filter(status__in=OPEN, expires_at__lte=timezone.now(), deleted=False):
        request.expire()
        expired += 1
    return expired


@shared_task
def send_reminders():
    """Remind, once, every signer whose turn it is when expiry is ``reminder_days`` away or less."""
    cutoff = timezone.now() + timedelta(days=get_setting("reminder_days"))
    reminded = 0
    for request in SignatureRequest.objects.filter(status__in=OPEN, expires_at__lte=cutoff, deleted=False):
        for signer in request.current_group():
            if signer.status in (Signer.Status.SENT, Signer.Status.VIEWED) and signer.reminded_at is None:
                signer.send_invite(reminder=True)
                reminded += 1
    return reminded

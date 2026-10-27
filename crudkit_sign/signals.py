"""Django signals fired by the signing flow. ``sender`` is always ``SignatureRequest``."""

from django.dispatch import Signal

request_sent = Signal()  # request
signer_viewed = Signal()  # request, signer
signer_signed = Signal()  # request, signer
request_completed = Signal()  # request
request_declined = Signal()  # request, signer, reason
request_voided = Signal()  # request, user, reason
request_expired = Signal()  # request

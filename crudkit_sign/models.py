"""Signature requests, signers, fields and the audit trail.

A SignatureRequest points at any crudkit object through a generic FK named
``related_object`` (the name the crudkit serializer knows how to render). The
flow lives on the models; ``service.create_request`` is the entry point.
"""

import hashlib
import secrets
from datetime import timedelta

from crudkit.decorators import crm_action
from crudkit.models import BaseCrudKitModel, CrudKitPositiveIntegerField
from crudkit.utils import UploadPathGenerator
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone

from crudkit_sign import mail, pdf, signals
from crudkit_sign.conf import get_setting
from crudkit_sign.signable import (
    CHECKBOX,
    DATE,
    IMAGE_KINDS,
    INITIALS,
    NAME,
    SIGNATURE,
    TEXT,
    SignatureError,
    call_hook,
)


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def unusable_token_hash():
    # A signer gets a real token the first time an invite goes out.
    return hash_token(secrets.token_urlsafe(32))


def _format_datetime(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S %Z") if value else ""


def check_groups(fields):
    """Each checkbox group allows one pick, and a required group needs it."""
    groups = {}
    for field in fields:
        if field.group:
            groups.setdefault(field.group, []).append(field)
    for group, members in groups.items():
        picked = sum(1 for field in members if field.value)
        if picked > 1:
            raise SignatureError(f"Choose only one option for '{group}'")
        if not picked and any(field.required for field in members):
            raise SignatureError(f"Choose an option for '{group}'")


class SignatureRequest(BaseCrudKitModel):
    TYPE_ID = "SRQ"

    class Status(models.TextChoices):
        DRAFT = "Draft"
        SENT = "Sent"
        PARTIALLY_SIGNED = "Partially signed"
        COMPLETED = "Completed"
        DECLINED = "Declined"
        VOIDED = "Voided"
        EXPIRED = "Expired"

    related_content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, editable=False)
    related_object_id = CrudKitPositiveIntegerField(editable=False)
    related_object = GenericForeignKey("related_content_type", "related_object_id")

    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, help_text="Shown to the signers in the invitation email")
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT, editable=False)
    source_file = models.FileField(upload_to=UploadPathGenerator("signatures/source"), editable=False)
    source_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    signed_file = models.FileField(upload_to=UploadPathGenerator("signatures/signed"), blank=True, editable=False)
    signed_sha256 = models.CharField(
        max_length=64, blank=True, editable=False, help_text="Hash of the signed pages, before the certificate"
    )
    sent_at = models.DateTimeField(null=True, blank=True, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    voided_reason = models.TextField(blank=True, editable=False)

    def __str__(self):
        return self.title or f"Signature request {self.pk}"

    @property
    def is_open(self):
        return self.status in (self.Status.SENT, self.Status.PARTIALLY_SIGNED)

    @property
    def download_open(self):
        """Whether the signing links may still serve the signed PDF."""
        if self.status != self.Status.COMPLETED or not self.signed_file or not self.completed_at:
            return False
        return timezone.now() < self.completed_at + timedelta(days=get_setting("download_days"))

    def log(self, kind, signer=None, ip_address=None, user_agent="", **detail):
        return SignatureEvent.objects.create(
            request=self,
            signer=signer,
            kind=kind,
            ip_address=ip_address or None,
            user_agent=(user_agent or "")[:512],
            detail=detail,
            created_by=self.updated_by,
            updated_by=self.updated_by,
        )

    def active_signers(self):
        return self.signer_set.filter(deleted=False).order_by("order", "id")

    def current_group(self):
        """Signers whose turn it is: the lowest order among those who have not signed or declined."""
        waiting = [s for s in self.active_signers() if s.status not in (Signer.Status.SIGNED, Signer.Status.DECLINED)]
        if not waiting:
            return []
        return [s for s in waiting if s.order == waiting[0].order]

    def notification_recipients(self):
        recipients = [signer.email for signer in self.active_signers()]
        if self.created_by.email:
            recipients.append(self.created_by.email)
        return list(dict.fromkeys(recipients))

    # -- transitions -------------------------------------------------------

    def send(self):
        """Email the signers whose turn it is. Safe to call again after a group completes."""
        if self.status in (self.Status.COMPLETED, self.Status.DECLINED, self.Status.VOIDED, self.Status.EXPIRED):
            raise SignatureError(f"Request is {self.status.lower()}")
        for signer in self.current_group():
            if signer.status == Signer.Status.PENDING:
                signer.send_invite()
        if self.status == self.Status.DRAFT:
            self.status = self.Status.SENT
            self.sent_at = timezone.now()
            self.save(update_fields=["status", "sent_at", "updated_at"])
            signals.request_sent.send(sender=SignatureRequest, request=self)
            call_hook(self.related_object, "on_signature_sent", self)

    def resend(self):
        """New links for everyone in the current group; earlier links stop working."""
        if not self.is_open:
            raise SignatureError(f"Request is {self.status.lower()}")
        for signer in self.current_group():
            signer.send_invite()

    def void(self, user, reason=""):
        if self.status in (self.Status.COMPLETED, self.Status.VOIDED):
            raise SignatureError(f"Request is already {self.status.lower()}")
        self.status = self.Status.VOIDED
        self.voided_reason = reason
        self.updated_by = user
        self.save(update_fields=["status", "voided_reason", "updated_by", "updated_at"])
        for signer in self.active_signers():
            signer.purge_signature()
        self.log(SignatureEvent.Kind.VOIDED, reason=reason, by=str(user))
        signals.request_voided.send(sender=SignatureRequest, request=self, user=user, reason=reason)
        call_hook(self.related_object, "on_signature_voided", self, user, reason)

    def expire(self):
        if not self.is_open:
            return
        self.status = self.Status.EXPIRED
        self.save(update_fields=["status", "updated_at"])
        self.log(SignatureEvent.Kind.EXPIRED)
        signals.request_expired.send(sender=SignatureRequest, request=self)
        call_hook(self.related_object, "on_signature_expired", self)

    def check_expiry(self):
        if self.is_open and self.expires_at and self.expires_at <= timezone.now():
            self.expire()

    def record_view(self, signer, ip_address=None, user_agent=""):
        if signer.viewed_at:
            return
        signer.viewed_at = timezone.now()
        if signer.status == Signer.Status.SENT:
            signer.status = Signer.Status.VIEWED
        signer.save(update_fields=["viewed_at", "status", "updated_at"])
        self.log(SignatureEvent.Kind.VIEWED, signer=signer, ip_address=ip_address, user_agent=user_agent)
        signals.signer_viewed.send(sender=SignatureRequest, request=self, signer=signer)
        call_hook(self.related_object, "on_signature_viewed", self, signer)

    def lock(self):
        """Row-lock this request and reload state; transitions racing us wait here."""
        SignatureRequest.objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()

    def sign(self, signer, values, image=None, image_kind="", ip_address=None, user_agent=""):
        """Record a signer's submission. ``values`` maps field pk to the submitted value."""
        with transaction.atomic():
            # Without the lock, the last two signers of a parallel group could
            # both see the group as done in advance() and complete() twice.
            self.lock()
            if not self.is_open:
                raise SignatureError(f"Request is {self.status.lower()}")
            if not signer.is_turn():
                raise SignatureError("It is not this signer's turn")
            fields = list(signer.fields())
            if any(f.kind in IMAGE_KINDS and f.required for f in fields) and not image:
                raise SignatureError("A signature is required")
            now = timezone.now()
            for field in fields:
                if field.kind == DATE:
                    field.value = timezone.localdate(now).isoformat()
                elif field.kind == NAME:
                    field.value = signer.name
                elif field.kind in (TEXT, CHECKBOX):
                    field.value = (values.get(str(field.pk)) or "").strip()
                    if field.required and not field.group and not field.value:
                        raise SignatureError(f"'{field.name or field.kind}' is required")
                field.save(update_fields=["value", "updated_at"])
            check_groups(fields)
            if image:
                signer.signature_image.save(f"{signer.pk}.png", ContentFile(image), save=False)
                signer.signature_kind = image_kind
            signer.consent_accepted_at = now
            signer.signed_at = now
            signer.status = Signer.Status.SIGNED
            signer.ip_address = ip_address or None
            signer.user_agent = (user_agent or "")[:512]
            signer.save()
            self.log(SignatureEvent.Kind.SIGNED, signer=signer, ip_address=ip_address, user_agent=user_agent)
            signals.signer_signed.send(sender=SignatureRequest, request=self, signer=signer)
            call_hook(self.related_object, "on_signature_signed", self, signer)
            self.advance()

    def advance(self):
        if self.current_group():
            self.status = self.Status.PARTIALLY_SIGNED
            self.save(update_fields=["status", "updated_at"])
            self.send()
        else:
            self.complete()

    def decline(self, signer, reason="", ip_address=None, user_agent=""):
        with transaction.atomic():
            self.lock()
            if not self.is_open:
                raise SignatureError(f"Request is {self.status.lower()}")
            signer.status = Signer.Status.DECLINED
            signer.declined_at = timezone.now()
            signer.decline_reason = reason
            signer.ip_address = ip_address or None
            signer.user_agent = (user_agent or "")[:512]
            signer.save()
            self.status = self.Status.DECLINED
            self.save(update_fields=["status", "updated_at"])
            self.log(
                SignatureEvent.Kind.DECLINED, signer=signer, ip_address=ip_address, user_agent=user_agent, reason=reason
            )
        signals.request_declined.send(sender=SignatureRequest, request=self, signer=signer, reason=reason)
        call_hook(self.related_object, "on_signature_declined", self, signer, reason)
        mail.send_declined(self, signer, reason, [self.created_by.email])

    def complete(self):
        self.source_file.open("rb")
        source = self.source_file.read()
        self.source_file.close()
        signed_pages = pdf.stamp(source, self.placements())
        self.signed_sha256 = pdf.sha256(signed_pages)
        self.completed_at = timezone.now()
        self.status = self.Status.COMPLETED
        certificate = pdf.certificate_page(self.certificate_data())
        signed = pdf.stamp(signed_pages, [], certificate)
        self.signed_file.save(f"{self.pk}-signed.pdf", ContentFile(signed), save=False)
        self.save()
        self.log(SignatureEvent.Kind.COMPLETED, signed_sha256=self.signed_sha256)
        signals.request_completed.send(sender=SignatureRequest, request=self)
        call_hook(self.related_object, "on_signature_completed", self, self.signed_file)
        mail.send_completed(self, self.notification_recipients(), signed)

    # -- data for stamping and the certificate ---------------------------------

    def placements(self):
        placements = []
        for signer in self.active_signers():
            if signer.status != Signer.Status.SIGNED:
                continue
            image = signer.signature_bytes()
            for field in signer.fields():
                placements.append(
                    pdf.Placement(
                        page=field.page,
                        x=field.x,
                        y=field.y,
                        width=field.width,
                        height=field.height,
                        kind=field.kind,
                        value=field.value,
                        image=image if field.kind in IMAGE_KINDS else None,
                    )
                )
        return placements

    def certificate_data(self):
        return {
            "title": self.title,
            "request_id": str(self.pk),
            "brand_name": get_setting("brand_name"),
            "created_at": _format_datetime(self.created_at),
            "completed_at": _format_datetime(self.completed_at),
            "source_sha256": self.source_sha256,
            "signed_sha256": self.signed_sha256,
            "signers": [
                {
                    "name": signer.name,
                    "email": signer.email,
                    "role": signer.role,
                    "status": signer.status,
                    "viewed_at": _format_datetime(signer.viewed_at),
                    "signed_at": _format_datetime(signer.signed_at),
                    "ip_address": signer.ip_address or "",
                    "consent_accepted_at": _format_datetime(signer.consent_accepted_at),
                }
                for signer in self.active_signers()
            ],
            "events": [
                {
                    "at": _format_datetime(event.created_at),
                    "kind": event.kind,
                    "who": f"{event.signer.name} <{event.signer.email}>" if event.signer else "",
                    "ip_address": event.ip_address or "",
                    "detail": ", ".join(f"{k}: {v}" for k, v in event.detail.items() if v),
                }
                for event in self.signatureevent_set.filter(deleted=False).order_by("created_at", "id")
            ],
        }

    # -- detail-page buttons ---------------------------------------------------

    @crm_action("Resend emails", requires_approval=True)
    def resend_action(self, request):
        try:
            self.resend()
        except SignatureError as error:
            return JsonResponse({"errors": [str(error)]}, status=400)
        return JsonResponse({"messages": ["New signing links sent; earlier links no longer work"]})

    @crm_action("Void", requires_approval=True)
    def void_action(self, request):
        try:
            self.void(request.user)
        except SignatureError as error:
            return JsonResponse({"errors": [str(error)]}, status=400)
        return self

    @crm_action("Preview unsigned PDF")
    def preview_source(self, request):
        return redirect(self.source_file.url)

    @crm_action("Download signed PDF")
    def download_signed(self, request):
        if not self.signed_file:
            return JsonResponse({"errors": ["Not signed yet"]}, status=400)
        return redirect(self.signed_file.url)

    class CrudKitSettings(BaseCrudKitModel.CrudKitSettings):
        search_fields = ["title"]
        allowed_prefills = ["related_object"]
        help_text = "One row per document sent for signature. Void and create a new one to correct anything."

    class Meta(BaseCrudKitModel.Meta):
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["related_content_type", "related_object_id"])]


class Signer(BaseCrudKitModel):
    TYPE_ID = "SGR"

    class Status(models.TextChoices):
        PENDING = "Pending"
        SENT = "Sent"
        VIEWED = "Viewed"
        SIGNED = "Signed"
        DECLINED = "Declined"

    class SignatureKind(models.TextChoices):
        DRAWN = "Drawn"
        TYPED = "Typed"

    request = models.ForeignKey(SignatureRequest, on_delete=models.CASCADE, editable=False)
    role = models.CharField(max_length=64, editable=False)
    name = models.CharField(max_length=255)
    email = models.EmailField()
    order = models.PositiveSmallIntegerField(default=1, help_text="Signers with the same order sign in parallel")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="signer_set"
    )
    note = models.TextField(blank=True, editable=False, help_text="Shown only to this signer in their invitation email")
    token_hash = models.CharField(max_length=64, unique=True, default=unusable_token_hash, editable=False)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING, editable=False)
    sent_at = models.DateTimeField(null=True, blank=True, editable=False)
    reminded_at = models.DateTimeField(null=True, blank=True, editable=False)
    viewed_at = models.DateTimeField(null=True, blank=True, editable=False)
    signed_at = models.DateTimeField(null=True, blank=True, editable=False)
    declined_at = models.DateTimeField(null=True, blank=True, editable=False)
    decline_reason = models.TextField(blank=True, editable=False)
    signature_image = models.FileField(upload_to=UploadPathGenerator("signatures/images"), blank=True, editable=False)
    signature_kind = models.CharField(max_length=16, choices=SignatureKind.choices, blank=True, editable=False)
    consent_accepted_at = models.DateTimeField(null=True, blank=True, editable=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True, editable=False)
    user_agent = models.CharField(max_length=512, blank=True, editable=False)

    def __str__(self):
        return f"{self.name} ({self.role})"

    def fields(self):
        return self.signaturefield_set.filter(deleted=False).order_by("page", "-y", "x")

    def is_turn(self):
        return any(signer.pk == self.pk for signer in self.request.current_group())

    def rotate_token(self):
        token = secrets.token_urlsafe(32)
        self.token_hash = hash_token(token)
        self.save(update_fields=["token_hash", "updated_at"])
        return token

    def send_invite(self, reminder=False):
        token = self.rotate_token()
        mail.send_invite(self, token, reminder=reminder)
        now = timezone.now()
        if reminder:
            self.reminded_at = now
            kind = SignatureEvent.Kind.REMINDED
        else:
            self.sent_at = now
            if self.status == self.Status.PENDING:
                self.status = self.Status.SENT
            kind = SignatureEvent.Kind.SENT
        self.save(update_fields=["sent_at", "reminded_at", "status", "updated_at"])
        self.request.log(kind, signer=self, email=self.email)

    def signature_bytes(self):
        if not self.signature_image:
            return None
        self.signature_image.open("rb")
        data = self.signature_image.read()
        self.signature_image.close()
        return data

    def purge_signature(self):
        if self.signature_image:
            self.signature_image.delete(save=True)

    @crm_action("Copy signing link")
    def signing_link(self, request):
        if not self.request.is_open:
            return JsonResponse({"errors": [f"Request is {self.request.status.lower()}"]}, status=400)
        if not self.is_turn():
            return JsonResponse({"errors": ["It is not this signer's turn yet"]}, status=400)
        token = self.rotate_token()
        self.request.log(SignatureEvent.Kind.LINK_COPIED, signer=self, by=str(request.user))
        return JsonResponse(
            {"messages": [f"Signing link for {self.name} (earlier links no longer work): {mail.signing_url(token)}"]}
        )

    class CrudKitSettings(BaseCrudKitModel.CrudKitSettings):
        search_fields = ["name", "email"]
        allowed_prefills = ["request"]

    class Meta(BaseCrudKitModel.Meta):
        ordering = ["order", "id"]


class SignatureField(BaseCrudKitModel):
    TYPE_ID = "SGF"

    class Kind(models.TextChoices):
        SIGNATURE = SIGNATURE
        INITIALS = INITIALS
        DATE = DATE
        NAME = NAME
        TEXT = TEXT
        CHECKBOX = CHECKBOX

    signer = models.ForeignKey(Signer, on_delete=models.CASCADE, editable=False)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    name = models.CharField(max_length=64, blank=True, help_text="Label for text and checkbox fields")
    page = models.PositiveIntegerField(help_text="1-based")
    x = models.FloatField(help_text="PDF points from the left edge")
    y = models.FloatField(help_text="PDF points from the bottom edge")
    width = models.FloatField()
    height = models.FloatField()
    required = models.BooleanField(default=True, blank=True)
    group = models.CharField(max_length=64, blank=True, help_text="Checkboxes sharing a group are a pick-one choice")
    anchor = models.CharField(max_length=128, blank=True, editable=False)
    value = models.TextField(blank=True, editable=False)

    def __str__(self):
        return f"{self.kind} on page {self.page}"

    def as_dict(self):
        return {
            "id": str(self.pk),
            "kind": self.kind,
            "name": self.name,
            "page": self.page,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "required": self.required,
            "group": self.group,
            "value": self.value,
        }

    class CrudKitSettings(BaseCrudKitModel.CrudKitSettings):
        allowed_prefills = ["signer"]

    class Meta(BaseCrudKitModel.Meta):
        ordering = ["page", "-y", "x"]


class SignatureEvent(BaseCrudKitModel):
    TYPE_ID = "SGE"

    class Kind(models.TextChoices):
        CREATED = "Created"
        SENT = "Sent"
        REMINDED = "Reminded"
        LINK_COPIED = "Link copied"
        VIEWED = "Viewed"
        SIGNED = "Signed"
        DECLINED = "Declined"
        VOIDED = "Voided"
        EXPIRED = "Expired"
        COMPLETED = "Completed"
        DOWNLOADED = "Downloaded"

    request = models.ForeignKey(SignatureRequest, on_delete=models.CASCADE, editable=False)
    signer = models.ForeignKey(Signer, on_delete=models.SET_NULL, null=True, blank=True, editable=False)
    kind = models.CharField(max_length=32, choices=Kind.choices, editable=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True, editable=False)
    user_agent = models.CharField(max_length=512, blank=True, editable=False)
    detail = models.JSONField(default=dict, blank=True, editable=False)

    def __str__(self):
        return f"{self.kind} {_format_datetime(self.created_at)}"

    class CrudKitSettings(BaseCrudKitModel.CrudKitSettings):
        allowed_prefills = ["request"]
        help_text = "Audit trail; printed as the certificate of completion."

    class Meta(BaseCrudKitModel.Meta):
        ordering = ["created_at", "id"]

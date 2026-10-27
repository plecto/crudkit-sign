"""Entry point for hosts: ``create_request(signable, user)``."""

from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from crudkit_sign.conf import get_setting
from crudkit_sign.models import SignatureEvent, SignatureField, SignatureRequest, Signer
from crudkit_sign.pdf import find_anchors, page_count, sha256
from crudkit_sign.signable import FIELD_KINDS, IMAGE_KINDS, Signable, SignatureError, call_hook


def _validate_signers(signers):
    if not signers:
        raise SignatureError("No signers")
    roles = [spec.role for spec in signers]
    if len(set(roles)) != len(roles):
        raise SignatureError("Signer roles must be unique")
    for spec in signers:
        if not spec.email:
            raise SignatureError(f"Signer '{spec.name or spec.role}' has no email address")
        if not spec.name:
            raise SignatureError(f"Signer for role '{spec.role}' has no name")


def _fields_by_role(signable, pdf_bytes, signers):
    get_fields = getattr(signable, "get_signature_fields", None)
    fields = list(get_fields()) if callable(get_fields) else find_anchors(pdf_bytes)
    pages = page_count(pdf_bytes)
    roles = {spec.role for spec in signers}
    by_role = {role: [] for role in roles}
    for field in fields:
        if field.role not in roles:
            raise SignatureError(f"Field for unknown role '{field.role}'")
        if field.kind not in FIELD_KINDS:
            raise SignatureError(f"Unknown field kind '{field.kind}'")
        if not 1 <= field.page <= pages:
            raise SignatureError(f"Field on page {field.page}, but the document has {pages} page(s)")
        by_role[field.role].append(field)
    for role, role_fields in by_role.items():
        if not any(field.kind in IMAGE_KINDS for field in role_fields):
            raise SignatureError(f"No signature field for role '{role}'; add a {{{{sig:{role}}}}} anchor to the PDF")
    return by_role


def create_request(signable, user, send=True, title="", message="", expiry_days=None):
    """Create a signature request for ``signable`` and, by default, email the first signers.

    Raises SignatureError when the signable is misconfigured (no signers, missing
    anchors, a field outside the document).
    """
    if not isinstance(signable, Signable):
        raise SignatureError(f"{type(signable).__name__} does not implement get_signature_pdf and get_signers")
    pdf_bytes = signable.get_signature_pdf()
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        raise SignatureError("get_signature_pdf did not return a PDF")
    signers = list(signable.get_signers())
    _validate_signers(signers)
    by_role = _fields_by_role(signable, pdf_bytes, signers)
    title = title or call_hook(signable, "get_signature_title") or str(signable)
    message = message or call_hook(signable, "get_signature_message") or ""
    days = expiry_days or call_hook(signable, "get_signature_expiry_days") or get_setting("expiry_days")
    audit = {"created_by": user, "updated_by": user}
    with transaction.atomic():
        request = SignatureRequest(
            related_object=signable,
            title=title[:255],
            message=message,
            expires_at=timezone.now() + timedelta(days=days),
            source_sha256=sha256(pdf_bytes),
            **audit,
        )
        request.source_file.save(f"{slugify(title) or 'document'}.pdf", ContentFile(pdf_bytes), save=False)
        request.save()
        for spec in signers:
            signer = Signer.objects.create(
                request=request,
                role=spec.role,
                name=spec.name,
                email=spec.email,
                order=spec.order,
                user_id=spec.user_id,
                note=spec.note,
                **audit,
            )
            for field in by_role[spec.role]:
                SignatureField.objects.create(
                    signer=signer,
                    kind=field.kind,
                    name=field.name,
                    page=field.page,
                    x=field.x,
                    y=field.y,
                    width=field.width,
                    height=field.height,
                    required=field.required,
                    group=field.group,
                    anchor=field.anchor,
                    **audit,
                )
        request.log(SignatureEvent.Kind.CREATED, by=str(user), source_sha256=request.source_sha256)
    if send:
        request.send()
    return request


def requests_for(signable):
    """All non-deleted requests for a signable object, newest first."""
    return SignatureRequest.objects.filter(
        related_content_type__model=signable._meta.model_name,
        related_content_type__app_label=signable._meta.app_label,
        related_object_id=signable.pk,
        deleted=False,
    )

"""Public signer pages. The token in the URL is the only credential."""

import base64
import json
from functools import wraps

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from crudkit_sign import throttle
from crudkit_sign.conf import get_setting
from crudkit_sign.models import SignatureEvent, SignatureRequest, Signer, hash_token
from crudkit_sign.signable import IMAGE_KINDS, SignatureError

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _branding():
    return {
        "brand_name": get_setting("brand_name"),
        "logo_url": get_setting("logo_url"),
        "consent_text": get_setting("consent_text"),
        "pdfjs_url": get_setting("pdfjs_url"),
        "pdfjs_worker_url": get_setting("pdfjs_worker_url"),
    }


def _unavailable(request, reason, status=410):
    return render(request, "crudkit_sign/unavailable.html", {**_branding(), "reason": reason}, status=status)


def with_signer(view):
    """Resolve the token, throttle, and hand the view a live signer or an explanatory page."""

    @wraps(view)
    def wrapper(request, token, *args, **kwargs):
        if not throttle.allow(request):
            return HttpResponse("Too many requests", status=429)
        signer = (
            Signer.objects.filter(token_hash=hash_token(token), deleted=False)
            .select_related("request", "request__created_by")
            .first()
        )
        if signer is None:
            raise Http404
        signer.request.check_expiry()
        response = view(request, signer, *args, **kwargs)
        # The URL is a credential: keep it out of search indexes and Referer headers, PDFs included.
        response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response["Referrer-Policy"] = "no-referrer"
        return response

    return wrapper


def _status_page(request, signer):
    """The page to show when the signer cannot sign right now, or None when they can."""
    signature_request = signer.request
    status = SignatureRequest.Status
    if signature_request.status == status.COMPLETED or signer.status == Signer.Status.SIGNED:
        return None  # handled by the caller: completed or done pages
    if signature_request.status == status.VOIDED:
        return _unavailable(request, "This document has been withdrawn by the sender.")
    if signature_request.status == status.EXPIRED:
        return _unavailable(request, "This signing link has expired. Ask the sender for a new one.")
    if signature_request.status == status.DECLINED:
        return _unavailable(request, "This document is no longer open for signing.")
    if signature_request.status == status.DRAFT:
        return _unavailable(request, "This document has not been sent yet.", status=404)
    if not signer.is_turn():
        return _unavailable(request, "It is not your turn to sign yet. You will get an email when it is.", status=409)
    return None


def _done_url(token):
    return reverse("crudkit_sign:done", args=[token])


def _render_done(request, signer, token):
    signature_request = signer.request
    context = {
        **_branding(),
        "signer": signer,
        "request_obj": signature_request,
        "completed": signature_request.status == SignatureRequest.Status.COMPLETED,
        "download_url": reverse("crudkit_sign:signed", args=[token]) if signature_request.download_open else "",
    }
    return render(request, "crudkit_sign/done.html", context)


@require_GET
@ensure_csrf_cookie
@with_signer
def sign_page(request, signer):
    token = request.resolver_match.kwargs["token"]
    if signer.status == Signer.Status.SIGNED or signer.request.status == SignatureRequest.Status.COMPLETED:
        return _render_done(request, signer, token)
    blocked = _status_page(request, signer)
    if blocked is not None:
        return blocked
    signer.request.record_view(signer, throttle.client_ip(request), request.META.get("HTTP_USER_AGENT", ""))
    completed = []
    for other in signer.request.active_signers():
        if other.status != Signer.Status.SIGNED:
            continue
        image = other.signature_bytes()
        image_url = "data:image/png;base64," + base64.b64encode(image).decode() if image else ""
        for field in other.fields():
            item = field.as_dict()
            item["image"] = image_url if field.kind in IMAGE_KINDS else ""
            completed.append(item)
    context = {
        **_branding(),
        "signer": signer,
        "request_obj": signer.request,
        "config": {
            "documentUrl": reverse("crudkit_sign:document", args=[token]),
            "submitUrl": reverse("crudkit_sign:submit", args=[token]),
            "declineUrl": reverse("crudkit_sign:decline", args=[token]),
            "signerName": signer.name,
            "today": timezone.localdate().isoformat(),
            "maxSignatureBytes": get_setting("max_signature_bytes"),
        },
        "fields": [field.as_dict() for field in signer.fields()],
        "completed": completed,
    }
    return render(request, "crudkit_sign/sign.html", context)


@require_GET
@with_signer
def document(request, signer):
    blocked = _status_page(request, signer)
    if blocked is not None and signer.status != Signer.Status.SIGNED:
        return blocked
    signer.request.source_file.open("rb")
    data = signer.request.source_file.read()
    signer.request.source_file.close()
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = 'inline; filename="document.pdf"'
    response["Cache-Control"] = "private, no-store"
    return response


def _decode_signature(data_url):
    if not data_url:
        return None
    if not data_url.startswith("data:image/png;base64,"):
        raise SignatureError("Signature must be a PNG")
    try:
        image = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except ValueError as error:
        raise SignatureError("Signature image is not valid") from error
    if not image.startswith(PNG_MAGIC):
        raise SignatureError("Signature must be a PNG")
    if len(image) > get_setting("max_signature_bytes"):
        raise SignatureError("Signature image is too large")
    return image


@require_POST
@with_signer
def submit(request, signer):
    blocked = _status_page(request, signer)
    if blocked is not None or signer.status == Signer.Status.SIGNED:
        return JsonResponse({"error": "This document cannot be signed right now"}, status=409)
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "Invalid request"}, status=400)
    if not payload.get("consent"):
        return JsonResponse({"error": "Please accept the consent statement"}, status=400)
    values = payload.get("fields") or {}
    if not isinstance(values, dict):
        return JsonResponse({"error": "Invalid request"}, status=400)
    kind = Signer.SignatureKind.TYPED if payload.get("signature_kind") == "Typed" else Signer.SignatureKind.DRAWN
    try:
        image = _decode_signature(payload.get("signature") or "")
        signer.request.sign(
            signer,
            {str(k): str(v) for k, v in values.items()},
            image=image,
            image_kind=kind,
            ip_address=throttle.client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except SignatureError as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse({"redirect": _done_url(request.resolver_match.kwargs["token"])})


@require_POST
@with_signer
def decline(request, signer):
    blocked = _status_page(request, signer)
    if blocked is not None or signer.status == Signer.Status.SIGNED:
        return JsonResponse({"error": "This document cannot be declined right now"}, status=409)
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "Invalid request"}, status=400)
    reason = str(payload.get("reason") or "")[:1000]
    try:
        signer.request.decline(signer, reason, throttle.client_ip(request), request.META.get("HTTP_USER_AGENT", ""))
    except SignatureError as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse({"redirect": _done_url(request.resolver_match.kwargs["token"])})


@require_GET
@with_signer
def done(request, signer):
    return _render_done(request, signer, request.resolver_match.kwargs["token"])


@require_GET
@with_signer
def signed_document(request, signer):
    signature_request = signer.request
    if signature_request.status != SignatureRequest.Status.COMPLETED or not signature_request.signed_file:
        raise Http404
    if not signature_request.download_open:
        return _unavailable(request, "This download link has expired. The signed document was emailed to you.")
    signature_request.signed_file.open("rb")
    data = signature_request.signed_file.read()
    signature_request.signed_file.close()
    signature_request.log(
        SignatureEvent.Kind.DOWNLOADED,
        signer=signer,
        ip_address=throttle.client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{signature_request.pk}-signed.pdf"'
    response["Cache-Control"] = "private, no-store"
    return response

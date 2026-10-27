"""Emails sent by the signing flow. Templates live in templates/crudkit_sign/emails/ and can be overridden."""

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from crudkit_sign.conf import get_setting


def signing_url(token):
    return get_setting("base_url").rstrip("/") + reverse("crudkit_sign:sign", args=[token])


def _base_context(request):
    return {
        "brand_name": get_setting("brand_name"),
        "logo_url": get_setting("logo_url"),
        "brand_color": get_setting("brand_color"),
        "request": request,
        "title": request.title,
        "message": request.message,
    }


def _send(template, context, recipients, attachments=()):
    recipients = [address for address in recipients if address]
    if not recipients:
        return
    subject = " ".join(render_to_string(f"crudkit_sign/emails/{template}_subject.txt", context).split())
    text = render_to_string(f"crudkit_sign/emails/{template}.txt", context)
    html = render_to_string(f"crudkit_sign/emails/{template}.html", context)
    email = EmailMultiAlternatives(subject, text, get_setting("from_email"), recipients)
    email.attach_alternative(html, "text/html")
    for filename, content, mimetype in attachments:
        email.attach(filename, content, mimetype)
    email.send()


def send_invite(signer, token, reminder=False):
    context = _base_context(signer.request)
    context.update({"signer": signer, "url": signing_url(token), "reminder": reminder})
    _send("invite", context, [signer.email])


def send_completed(request, recipients, signed_pdf):
    filename = f"{request.pk}-signed.pdf"
    _send("completed", _base_context(request), recipients, [(filename, signed_pdf, "application/pdf")])


def send_declined(request, signer, reason, recipients):
    context = _base_context(request)
    context.update({"signer": signer, "reason": reason})
    _send("declined", context, recipients)

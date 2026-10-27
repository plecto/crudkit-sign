"""Settings for the addon, read from ``settings.CRUDKIT_SIGN`` with defaults."""

from django.conf import settings

PDFJS_VERSION = "4.10.38"

DEFAULTS = {
    # Shown on the signer page and in emails.
    "brand_name": "",
    # Emails need a PNG or JPG: Gmail and Outlook don't show SVG images.
    "logo_url": "",
    # Background of the button in the invitation email; it has white text.
    "brand_color": "#111827",
    # Absolute origin used to build signing links in emails, e.g. "https://crm.example.com".
    # Falls back to settings.BASE_URL when unset.
    "base_url": "",
    # Sender for signing emails; falls back to settings.DEFAULT_FROM_EMAIL.
    "from_email": "",
    "consent_text": (
        "By ticking this box I agree to sign this document electronically and that my electronic "
        "signature is the legal equivalent of my handwritten signature."
    ),
    "expiry_days": 30,
    # Days before expiry at which an unsigned signer is reminded once.
    "reminder_days": 3,
    # Days after completion the signed PDF stays downloadable from the signing link.
    # Everyone gets it as an email attachment, so the link only needs to outlive the done page.
    "download_days": 3,
    # Requests per minute per client IP on the public signer views.
    "throttle_per_minute": 60,
    # Reverse proxies in front of Django that append to X-Forwarded-For. 0 means
    # ignore the header (it is client-controlled) and use REMOTE_ADDR; the
    # throttle and the audit-trail IPs are only as trustworthy as this value.
    "trusted_proxy_hops": 0,
    # pdf.js is loaded from a CDN by default; point these at self-hosted copies to avoid it.
    "pdfjs_url": f"https://cdnjs.cloudflare.com/ajax/libs/pdf.js/{PDFJS_VERSION}/pdf.min.mjs",
    "pdfjs_worker_url": f"https://cdnjs.cloudflare.com/ajax/libs/pdf.js/{PDFJS_VERSION}/pdf.worker.min.mjs",
    # Largest signature PNG accepted from the browser.
    "max_signature_bytes": 200_000,
}


def get_setting(name):
    value = getattr(settings, "CRUDKIT_SIGN", {}).get(name, DEFAULTS[name])
    if name == "base_url" and not value:
        value = getattr(settings, "BASE_URL", "")
    if name == "from_email" and not value:
        value = settings.DEFAULT_FROM_EMAIL
    return value

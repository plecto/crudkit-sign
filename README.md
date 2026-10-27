# crudkit-sign

E-signatures for [crudkit](https://github.com/fenerum/crudkit) models. Hand it a
PDF and a list of signers; it emails tokenised signing links, shows the document
on a public page where each signer draws or types a signature, stamps the
signatures into the PDF, appends a certificate of completion and tells your
model when it is done. No third-party e-signature service involved.

Signature level: simple electronic signature (eIDAS SES). Consent text, IP,
user agent, timestamps and document hashes are recorded per signer.

## Install

```
pip install crudkit-sign
```

```python
INSTALLED_APPS = [..., "crudkit", "crudkit_sign", ...]

# urls.py, before the crudkit SPA catch-all
path("sign/", include("crudkit_sign.urls"))

CRUDKIT_SIGN = {
    "brand_name": "Example Inc",
    "logo_url": "https://crm.example.com/static/logo.png",  # PNG/JPG: email clients drop SVG
    "brand_color": "#111827",  # the email's "Review and sign" button, white text on it
    "base_url": "https://crm.example.com",  # used in email links; falls back to settings.BASE_URL
    "from_email": "sign@example.com",  # falls back to DEFAULT_FROM_EMAIL
    "expiry_days": 30,
    "reminder_days": 3,
    "download_days": 3,  # how long the signing link serves the signed PDF after completion
}
```

Run `migrate`. Configure Django's `EMAIL_BACKEND`. Schedule
`crudkit_sign.tasks.expire_requests` (hourly) and
`crudkit_sign.tasks.send_reminders` (daily) with Celery beat.

pdf.js is loaded from cdnjs by default. Set `pdfjs_url` and `pdfjs_worker_url`
in `CRUDKIT_SIGN` to self-hosted copies if you would rather not.

## Make a model signable

```python
from crudkit_sign.signable import SignerSpec


class Contract(BaseCrudKitModel):
    TYPE_ID = "CNT"

    def get_signature_pdf(self) -> bytes:
        return render_my_pdf(self)

    def get_signers(self) -> list[SignerSpec]:
        return [
            SignerSpec(role="customer", name="Ada Lovelace", email="ada@example.com", order=1),
            SignerSpec(role="company", name="Bob Smith", email="bob@example.com", order=2, user_id=bob.pk),
        ]

    # Optional
    def get_signature_title(self): ...
    def get_signature_message(self): ...
    def get_signature_expiry_days(self): ...
    def get_signature_fields(self): ...  # explicit FieldSpec list instead of anchors
    def on_signature_sent(self, request): ...
    def on_signature_viewed(self, request, signer): ...
    def on_signature_signed(self, request, signer): ...
    def on_signature_completed(self, request, signed_file): ...
    def on_signature_declined(self, request, signer, reason): ...
    def on_signature_voided(self, request, user, reason): ...
    def on_signature_expired(self, request): ...
```

Signers with the same `order` sign in parallel; a higher order waits for the
lower ones. A `SignerSpec(note=...)` is shown only to that signer, in
their invitation email. Django signals in `crudkit_sign.signals` fire alongside the hooks.

Then, typically from a `@crm_action`:

```python
from crudkit_sign.service import create_request

request = create_request(contract, request.user)  # emails the first signers
```

## Placing fields

Write anchor strings into the PDF where the fields go, in white 1pt text so
readers never see them:

| Anchor | Field |
|---|---|
| `{{sig:customer}}` | signature image |
| `{{initials:customer}}` | initials (same image, smaller box) |
| `{{date:customer}}` | date of signing, filled by the server |
| `{{name:customer}}` | signer's name, filled by the server |
| `{{text:customer:po_number}}` | free text the signer types |
| `{{check:customer:terms}}` | checkbox the signer ticks |

The part after the first colon is the signer role and must match a
`SignerSpec.role`. The box is placed with its bottom-left corner at the anchor.
`crudkit_sign.pdf.anchor_text(kind, role, name)` builds the string for you.
Every role needs at least one signature or initials anchor.

Checkboxes whose `FieldSpec`s share a `group` are a pick-one choice (radio
buttons on the signer page); a required group needs exactly one picked. Set it
by overriding `get_signature_fields()`, e.g. to make a yes/no question required.

If you know the coordinates, skip anchors and return `FieldSpec`s from
`get_signature_fields()` (PDF points, origin bottom-left, 1-based page).

## What you get in the crudkit SPA

Four crudkit models with the usual list, detail, search and changelog:
`SignatureRequest` (SRQ), `Signer` (SGR), `SignatureField` (SGF) and
`SignatureEvent` (SGE). Add `["SRQ", [...]]` to your model's `Layout.inlines`
to see requests on its detail page.

Detail-page actions: Resend emails, Void, Preview unsigned PDF and Download
signed PDF on a request; Copy signing link on a signer. Resend and Copy link
rotate the signer's token, so earlier links stop working.

## Security notes

- The token is the only credential: 256 bits, stored as a SHA-256 hash.
- Public views are throttled per client IP (`throttle_per_minute`).
- Links stop working at `expires_at` while open. After completion they serve
  the signed PDF for `download_days` only; everyone also gets it by email.
- Public responses send `X-Robots-Tag: noindex` and `Referrer-Policy: no-referrer`.
- X-Forwarded-For is ignored unless `trusted_proxy_hops` is set to the number
  of reverse proxies in front of Django; the throttle key and the audit-trail
  IPs are only as trustworthy as that setting.
- The signed file is written once. To correct anything, void and create a new
  request.
- `signed_sha256` is the hash of the stamped pages before the certificate was
  appended; the certificate prints it.
- Voiding deletes the stored signature images.

## Development

```
python runtests.py
```

The tests use sqlite and a fixture model in `tests/testapp`.

## License

MIT.

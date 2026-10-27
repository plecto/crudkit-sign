"""A minimal Signable used by the addon's tests. It records every hook call."""

import io

from crudkit.models import BaseCrudKitModel
from django.db import models
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from crudkit_sign.signable import SignerSpec


def build_pdf(anchors, pages=1):
    """A PDF with visible body text and the given anchor strings drawn white and tiny."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for page in range(1, pages + 1):
        pdf.setFont("Helvetica", 12)
        pdf.setFillColorRGB(0, 0, 0)
        pdf.drawString(72, 750, f"Agreement page {page}")
        pdf.setFillColorRGB(1, 1, 1)
        pdf.setFont("Helvetica", 1)
        for index, anchor in enumerate(anchors if page == pages else []):
            pdf.drawString(72, 200 - index * 40, anchor)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class Memo(BaseCrudKitModel):
    TYPE_ID = "TMO"

    title = models.CharField(max_length=255)
    anchors = models.JSONField(default=list)
    signer_specs = models.JSONField(default=list)
    pages = models.PositiveIntegerField(default=1)
    hook_calls = models.JSONField(default=list)

    def __str__(self):
        return self.title

    def get_signature_pdf(self):
        return build_pdf(self.anchors, self.pages)

    def get_signers(self):
        return [SignerSpec(**spec) for spec in self.signer_specs]

    def get_signature_message(self):
        return "Please sign."

    def _record(self, name):
        self.refresh_from_db(fields=["hook_calls"])
        Memo.objects.filter(pk=self.pk).update(hook_calls=[*self.hook_calls, name])
        self.refresh_from_db(fields=["hook_calls"])

    def on_signature_sent(self, request):
        self._record("sent")

    def on_signature_viewed(self, request, signer):
        self._record("viewed")

    def on_signature_signed(self, request, signer):
        self._record("signed")

    def on_signature_completed(self, request, signed_file):
        self._record("completed")

    def on_signature_declined(self, request, signer, reason):
        self._record("declined")

    def on_signature_voided(self, request, user, reason):
        self._record("voided")

    def on_signature_expired(self, request):
        self._record("expired")

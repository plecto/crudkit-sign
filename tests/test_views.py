import base64
import json
from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from crudkit_sign.models import SignatureEvent, SignatureRequest, Signer
from crudkit_sign.service import create_request
from tests.helpers import hooks, last_token, make_memo, make_user, png_bytes

TWO_SIGNERS = [
    {"role": "approver", "name": "Bob", "email": "bob@example.com", "order": 1},
    {"role": "customer", "name": "Ada", "email": "ada@example.com", "order": 2},
]
ANCHORS = ["{{sig:approver}}", "{{sig:customer}}", "{{date:customer}}", "{{check:customer:terms}}"]


def data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


class SignerPageTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.memo = make_memo(self.user, anchors=ANCHORS, signers=TWO_SIGNERS)
        self.request = create_request(self.memo, self.user)
        self.bob, self.ada = self.request.active_signers()
        self.bob_token = last_token("bob@example.com")
        cache.clear()

    def post_json(self, url, payload, **extra):
        return self.client.post(url, json.dumps(payload), content_type="application/json", **extra)

    def test_unknown_token_is_404(self):
        self.assertEqual(self.client.get("/sign/nope/").status_code, 404)

    def test_page_renders_and_records_view(self):
        response = self.client.get(f"/sign/{self.bob_token}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bob")
        self.assertContains(response, "Order form")
        self.assertContains(response, 'id="cs-fields"')
        self.assertEqual([f["kind"] for f in response.context["fields"]], ["Signature"])
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.status, Signer.Status.VIEWED)
        self.assertIsNotNone(self.bob.viewed_at)
        self.assertEqual(hooks(self.memo), ["sent", "viewed"])
        # A second visit does not log another view.
        self.client.get(f"/sign/{self.bob_token}/")
        self.assertEqual(self.request.signatureevent_set.filter(kind=SignatureEvent.Kind.VIEWED).count(), 1)

    def test_document_is_served_to_the_signer(self):
        response = self.client.get(f"/sign/{self.bob_token}/document.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_second_signer_must_wait(self):
        self.ada.token_hash = Signer.objects.get(pk=self.ada.pk).token_hash
        token = self.ada.rotate_token()
        response = self.client.get(f"/sign/{token}/")
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "not your turn", status_code=409)

    def test_submit_requires_consent_and_signature(self):
        response = self.post_json(f"/sign/{self.bob_token}/submit/", {"consent": False})
        self.assertEqual(response.status_code, 400)
        self.assertIn("consent", response.json()["error"])
        response = self.post_json(f"/sign/{self.bob_token}/submit/", {"consent": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("signature is required", response.json()["error"])
        response = self.post_json(
            f"/sign/{self.bob_token}/submit/", {"consent": True, "signature": "data:image/jpeg;base64,AAAA"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("PNG", response.json()["error"])

    def test_full_flow_through_the_views(self):
        response = self.post_json(
            f"/sign/{self.bob_token}/submit/",
            {"consent": True, "signature": data_url(png_bytes()), "signature_kind": "Drawn"},
            HTTP_USER_AGENT="Test UA",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect"], f"/sign/{self.bob_token}/done/")
        response = self.client.get(f"/sign/{self.bob_token}/done/")
        self.assertContains(response, "Your signature has been recorded")
        # Signing again is refused, and the page shows the done state.
        self.assertEqual(self.post_json(f"/sign/{self.bob_token}/submit/", {"consent": True}).status_code, 409)
        self.assertContains(self.client.get(f"/sign/{self.bob_token}/"), "Your signature has been recorded")

        ada_token = last_token("ada@example.com")
        response = self.client.get(f"/sign/{ada_token}/")
        self.assertEqual(response.status_code, 200)
        # Bob's signature is shown to Ada as a completed overlay.
        self.assertEqual(len(response.context["completed"]), 1)
        self.assertTrue(response.context["completed"][0]["image"].startswith("data:image/png;base64,"))
        terms = self.ada.fields().get(kind="Checkbox")
        response = self.post_json(
            f"/sign/{ada_token}/submit/",
            {"consent": True, "signature": data_url(png_bytes()), "signature_kind": "Typed", "fields": {}},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("'terms' is required", response.json()["error"])
        response = self.post_json(
            f"/sign/{ada_token}/submit/",
            {
                "consent": True,
                "signature": data_url(png_bytes()),
                "signature_kind": "Typed",
                "fields": {str(terms.pk): "true"},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, SignatureRequest.Status.COMPLETED)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.signature_kind, Signer.SignatureKind.TYPED)

        response = self.client.get(f"/sign/{ada_token}/done/")
        self.assertContains(response, "All signatures collected")
        response = self.client.get(f"/sign/{ada_token}/signed.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(self.request.signatureevent_set.last().kind, SignatureEvent.Kind.DOWNLOADED)
        # Bob's link keeps working for the download too.
        self.assertEqual(self.client.get(f"/sign/{self.bob_token}/signed.pdf").status_code, 200)

        SignatureRequest.objects.filter(pk=self.request.pk).update(completed_at=timezone.now() - timedelta(days=3))
        self.assertEqual(self.client.get(f"/sign/{ada_token}/signed.pdf").status_code, 410)
        response = self.client.get(f"/sign/{ada_token}/done/")
        self.assertContains(response, "All signatures collected")
        self.assertNotContains(response, "signed.pdf")

    def test_decline_through_the_view(self):
        mail.outbox.clear()
        response = self.post_json(f"/sign/{self.bob_token}/decline/", {"reason": "No"})
        self.assertEqual(response.status_code, 200)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, SignatureRequest.Status.DECLINED)
        self.assertEqual(mail.outbox[-1].to, ["staff@example.com"])
        self.assertContains(self.client.get(f"/sign/{self.bob_token}/done/"), "You declined")
        self.assertEqual(self.client.get(f"/sign/{self.bob_token}/document.pdf").status_code, 410)

    def test_responses_are_not_indexed_or_referred(self):
        for url in (f"/sign/{self.bob_token}/", f"/sign/{self.bob_token}/document.pdf"):
            response = self.client.get(url)
            self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow, noarchive")
            self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_expired_link(self):
        SignatureRequest.objects.filter(pk=self.request.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        response = self.client.get(f"/sign/{self.bob_token}/")
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "expired", status_code=410)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, SignatureRequest.Status.EXPIRED)

    def test_voided_link(self):
        self.request.void(self.user)
        response = self.client.get(f"/sign/{self.bob_token}/")
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "withdrawn", status_code=410)

    def test_old_link_stops_working_after_resend(self):
        self.request.resend()
        self.assertEqual(self.client.get(f"/sign/{self.bob_token}/").status_code, 404)
        self.assertEqual(self.client.get(f"/sign/{last_token('bob@example.com')}/").status_code, 200)

    @override_settings(CRUDKIT_SIGN={"brand_name": "Example Inc", "throttle_per_minute": 2})
    def test_throttle(self):
        self.assertEqual(self.client.get(f"/sign/{self.bob_token}/document.pdf").status_code, 200)
        self.assertEqual(self.client.get(f"/sign/{self.bob_token}/document.pdf").status_code, 200)
        self.assertEqual(self.client.get(f"/sign/{self.bob_token}/document.pdf").status_code, 429)

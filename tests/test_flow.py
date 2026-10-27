import io
from datetime import timedelta

from crudkit.authorization import requires_approval
from django.core import mail
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from pypdf import PdfReader

from crudkit_sign import signals
from crudkit_sign.models import SignatureEvent, SignatureRequest, Signer
from crudkit_sign.service import create_request, requests_for
from crudkit_sign.signable import SignatureError
from crudkit_sign.tasks import expire_requests, send_reminders
from tests.helpers import hooks, make_memo, make_user, png_bytes

TWO_SIGNERS = [
    {"role": "approver", "name": "Bob", "email": "bob@example.com", "order": 1},
    {"role": "customer", "name": "Ada", "email": "ada@example.com", "order": 2},
]
TWO_SIGNER_ANCHORS = ["{{sig:approver}}", "{{sig:customer}}", "{{date:customer}}", "{{text:customer:po}}"]


class CreateRequestTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_creates_signers_fields_and_emails_first_group(self):
        memo = make_memo(self.user, anchors=TWO_SIGNER_ANCHORS, signers=TWO_SIGNERS)
        request = create_request(memo, self.user)

        self.assertEqual(request.status, SignatureRequest.Status.SENT)
        self.assertEqual(request.related_object, memo)
        self.assertEqual(request.title, "Order form")
        self.assertEqual(request.message, "Please sign.")
        self.assertEqual(len(request.source_sha256), 64)
        self.assertTrue(request.source_file.name.endswith(".pdf"))
        self.assertAlmostEqual(request.expires_at, timezone.now() + timedelta(days=30), delta=timedelta(minutes=1))
        bob, ada = request.active_signers()
        self.assertEqual(bob.status, Signer.Status.SENT)
        self.assertEqual(ada.status, Signer.Status.PENDING)
        self.assertEqual([f.kind for f in bob.fields()], ["Signature"])
        self.assertEqual({f.kind for f in ada.fields()}, {"Signature", "Date", "Text"})
        self.assertEqual(ada.fields().get(kind="Text").name, "po")
        self.assertEqual([m.to for m in mail.outbox], [["bob@example.com"]])
        self.assertIn("Please sign.", mail.outbox[0].body)
        self.assertEqual(hooks(memo), ["sent"])
        self.assertEqual(
            list(request.signatureevent_set.values_list("kind", flat=True)),
            [SignatureEvent.Kind.CREATED, SignatureEvent.Kind.SENT],
        )
        self.assertEqual(list(requests_for(memo)), [request])

    def test_note_is_only_shown_to_its_signer(self):
        signers = [{**TWO_SIGNERS[0], "note": "Discount over 30%"}, {**TWO_SIGNERS[1], "order": 1}]
        memo = make_memo(self.user, anchors=TWO_SIGNER_ANCHORS, signers=signers)
        create_request(memo, self.user)
        mails = {m.to[0]: m for m in mail.outbox}
        self.assertIn("Discount over 30%", mails["bob@example.com"].body)
        self.assertIn("Discount over 30%", mails["bob@example.com"].alternatives[0][0])
        self.assertNotIn("Discount over 30%", mails["ada@example.com"].body)
        self.assertNotIn("Discount over 30%", mails["ada@example.com"].alternatives[0][0])

    def test_send_false_leaves_a_draft(self):
        memo = make_memo(self.user)
        request = create_request(memo, self.user, send=False)
        self.assertEqual(request.status, SignatureRequest.Status.DRAFT)
        self.assertEqual(mail.outbox, [])
        request.send()
        self.assertEqual(request.status, SignatureRequest.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)

    def test_invite_escapes_html_but_not_text(self):
        memo = make_memo(self.user)
        request = create_request(memo, self.user, send=False)
        request.message = "Don't <b>wait</b>"
        request.send()
        self.assertIn("Don't <b>wait</b>", mail.outbox[0].body)
        self.assertIn("Don&#x27;t &lt;b&gt;wait&lt;/b&gt;", mail.outbox[0].alternatives[0][0])

    def test_missing_signature_anchor_is_an_error(self):
        memo = make_memo(self.user, anchors=["{{date:customer}}"])
        with self.assertRaisesMessage(SignatureError, "No signature field for role 'customer'"):
            create_request(memo, self.user)
        self.assertFalse(SignatureRequest.objects.exists())

    def test_anchor_for_unknown_role_is_an_error(self):
        memo = make_memo(self.user, anchors=["{{sig:customer}}", "{{sig:ghost}}"])
        with self.assertRaisesMessage(SignatureError, "unknown role 'ghost'"):
            create_request(memo, self.user)

    def test_signer_without_email_is_an_error(self):
        memo = make_memo(self.user, signers=[{"role": "customer", "name": "Ada", "email": ""}])
        with self.assertRaisesMessage(SignatureError, "no email"):
            create_request(memo, self.user)

    def test_non_signable_is_an_error(self):
        with self.assertRaises(SignatureError):
            create_request(self.user, self.user)


class SigningFlowTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.memo = make_memo(self.user, anchors=TWO_SIGNER_ANCHORS, signers=TWO_SIGNERS)
        self.request = create_request(self.memo, self.user)
        self.bob, self.ada = self.request.active_signers()
        self.received = []
        for name in ("signer_signed", "request_completed", "request_declined", "request_voided", "request_expired"):
            getattr(signals, name).connect(self._receiver(name), weak=False)

    def _receiver(self, name):
        def receive(sender, **kwargs):
            self.received.append(name)

        return receive

    def test_sequential_signing_completes_and_stamps(self):
        self.assertFalse(self.ada.is_turn())
        with self.assertRaisesMessage(SignatureError, "not this signer's turn"):
            self.request.sign(self.ada, {}, image=png_bytes())

        self.request.record_view(self.bob, "10.0.0.1", "Test UA")
        self.request.sign(self.bob, {}, image=png_bytes(), image_kind="Drawn", ip_address="10.0.0.1")
        self.request.refresh_from_db()
        self.bob.refresh_from_db()
        self.ada.refresh_from_db()
        self.assertEqual(self.request.status, SignatureRequest.Status.PARTIALLY_SIGNED)
        self.assertEqual(self.bob.status, Signer.Status.SIGNED)
        self.assertEqual(self.bob.ip_address, "10.0.0.1")
        self.assertIsNotNone(self.bob.consent_accepted_at)
        self.assertEqual(self.ada.status, Signer.Status.SENT)
        self.assertEqual([m.to for m in mail.outbox], [["bob@example.com"], ["ada@example.com"]])

        po = self.ada.fields().get(kind="Text")
        with self.assertRaisesMessage(SignatureError, "'po' is required"):
            self.request.sign(self.ada, {}, image=png_bytes())
        with self.assertRaisesMessage(SignatureError, "A signature is required"):
            self.request.sign(self.ada, {str(po.pk): "PO-1"})
        self.request.sign(self.ada, {str(po.pk): "PO-1"}, image=png_bytes(), image_kind="Typed")

        self.request.refresh_from_db()
        self.assertEqual(self.request.status, SignatureRequest.Status.COMPLETED)
        self.assertIsNotNone(self.request.completed_at)
        self.assertEqual(len(self.request.signed_sha256), 64)
        self.assertEqual(self.ada.fields().get(kind="Date").value, timezone.localdate().isoformat())
        self.assertEqual(self.ada.fields().get(kind="Text").value, "PO-1")
        reader = PdfReader(io.BytesIO(self.request.signed_file.read()))
        self.assertEqual(len(reader.pages), 2)  # one source page plus the certificate
        self.assertIn("PO-1", reader.pages[0].extract_text())
        self.assertIn("Certificate of completion", reader.pages[1].extract_text())
        self.assertIn("ada@example.com", reader.pages[1].extract_text())
        # Completion email to both signers and the sender, with the PDF attached.
        self.assertEqual(mail.outbox[-1].to, ["bob@example.com", "ada@example.com", "staff@example.com"])
        self.assertEqual(mail.outbox[-1].attachments[0][2], "application/pdf")
        self.assertEqual(hooks(self.memo), ["sent", "viewed", "signed", "signed", "completed"])
        self.assertEqual(self.received, ["signer_signed", "signer_signed", "request_completed"])
        kinds = list(self.request.signatureevent_set.values_list("kind", flat=True))
        self.assertEqual(
            kinds,
            [
                SignatureEvent.Kind.CREATED,
                SignatureEvent.Kind.SENT,
                SignatureEvent.Kind.VIEWED,
                SignatureEvent.Kind.SIGNED,
                SignatureEvent.Kind.SENT,
                SignatureEvent.Kind.SIGNED,
                SignatureEvent.Kind.COMPLETED,
            ],
        )

    def test_parallel_signers_are_emailed_together(self):
        memo = make_memo(
            self.user,
            anchors=["{{sig:a}}", "{{sig:b}}"],
            signers=[
                {"role": "a", "name": "A", "email": "a@example.com"},
                {"role": "b", "name": "B", "email": "b@example.com"},
            ],
        )
        mail.outbox.clear()
        request = create_request(memo, self.user)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["a@example.com", "b@example.com"])
        a, b = request.active_signers()
        self.assertTrue(a.is_turn() and b.is_turn())
        request.sign(b, {}, image=png_bytes())
        request.refresh_from_db()
        self.assertEqual(request.status, SignatureRequest.Status.PARTIALLY_SIGNED)
        request.sign(a, {}, image=png_bytes())
        request.refresh_from_db()
        self.assertEqual(request.status, SignatureRequest.Status.COMPLETED)

    def test_checkbox_group_needs_exactly_one_pick(self):
        memo = make_memo(self.user, anchors=["{{sig:customer}}", "{{check:customer:no}}", "{{check:customer:yes}}"])
        request = create_request(memo, self.user)
        (signer,) = request.active_signers()
        signer.fields().filter(kind="Checkbox").update(group="po")
        no, yes = (str(signer.fields().get(name=name).pk) for name in ("no", "yes"))
        with self.assertRaisesMessage(SignatureError, "Choose an option for 'po'"):
            request.sign(signer, {}, image=png_bytes())
        with self.assertRaisesMessage(SignatureError, "Choose only one option for 'po'"):
            request.sign(signer, {no: "true", yes: "true"}, image=png_bytes())
        request.sign(signer, {yes: "true"}, image=png_bytes())
        self.assertEqual(signer.fields().get(name="yes").value, "true")
        self.assertEqual(signer.fields().get(name="no").value, "")

    def test_signing_twice_is_refused(self):
        self.request.sign(self.bob, {}, image=png_bytes())
        with self.assertRaisesMessage(SignatureError, "not this signer's turn"):
            self.request.sign(self.bob, {}, image=png_bytes())

    def test_decline_closes_the_request_and_tells_the_sender(self):
        self.request.decline(self.bob, "Wrong price", "10.0.0.2")
        self.request.refresh_from_db()
        self.bob.refresh_from_db()
        self.assertEqual(self.request.status, SignatureRequest.Status.DECLINED)
        self.assertEqual(self.bob.status, Signer.Status.DECLINED)
        self.assertEqual(self.bob.decline_reason, "Wrong price")
        self.assertEqual(mail.outbox[-1].to, ["staff@example.com"])
        self.assertIn("Wrong price", mail.outbox[-1].body)
        self.assertEqual(hooks(self.memo), ["sent", "declined"])
        self.assertIn("request_declined", self.received)
        with self.assertRaises(SignatureError):
            self.request.sign(self.bob, {}, image=png_bytes())

    def test_void_purges_signature_images(self):
        self.request.sign(self.bob, {}, image=png_bytes())
        self.bob.refresh_from_db()
        self.assertTrue(self.bob.signature_image)
        self.request.void(self.user, "Sent to the wrong person")
        self.request.refresh_from_db()
        self.bob.refresh_from_db()
        self.assertEqual(self.request.status, SignatureRequest.Status.VOIDED)
        self.assertEqual(self.request.voided_reason, "Sent to the wrong person")
        self.assertFalse(self.bob.signature_image)
        self.assertEqual(hooks(self.memo)[-1], "voided")
        with self.assertRaises(SignatureError):
            self.request.void(self.user)
        with self.assertRaises(SignatureError):
            self.request.resend()

    def test_resend_rotates_tokens(self):
        before = self.bob.token_hash
        mail.outbox.clear()
        self.request.resend()
        self.bob.refresh_from_db()
        self.assertNotEqual(self.bob.token_hash, before)
        self.assertEqual([m.to for m in mail.outbox], [["bob@example.com"]])

    def test_expiry_task(self):
        SignatureRequest.objects.filter(pk=self.request.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(expire_requests(), 1)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, SignatureRequest.Status.EXPIRED)
        self.assertEqual(hooks(self.memo)[-1], "expired")
        self.assertIn("request_expired", self.received)
        self.assertEqual(expire_requests(), 0)

    def test_reminder_task_reminds_current_group_once(self):
        self.assertEqual(send_reminders(), 0)
        SignatureRequest.objects.filter(pk=self.request.pk).update(expires_at=timezone.now() + timedelta(days=2))
        mail.outbox.clear()
        self.assertEqual(send_reminders(), 1)
        self.assertEqual(mail.outbox[0].to, ["bob@example.com"])
        self.assertIn("Reminder", mail.outbox[0].subject)
        self.assertEqual(send_reminders(), 0)
        self.bob.refresh_from_db()
        self.assertIsNotNone(self.bob.reminded_at)


class ActionTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.memo = make_memo(self.user)
        self.request = create_request(self.memo, self.user)
        self.signer = self.request.active_signers().get()

    def test_actions_are_registered(self):
        self.assertEqual(
            set(self.request._actions), {"resend_action", "void_action", "preview_source", "download_signed"}
        )
        self.assertEqual(set(self.signer._actions), {"signing_link"})

    def test_copy_signing_link_rotates_and_returns_url(self):
        class FakeRequest:
            user = self.user

        before = self.signer.token_hash
        response = self.signer.signing_link(FakeRequest())
        self.signer.refresh_from_db()
        self.assertNotEqual(self.signer.token_hash, before)
        self.assertIn("https://crm.example.com/sign/", response.content.decode())
        self.assertEqual(self.request.signatureevent_set.last().kind, SignatureEvent.Kind.LINK_COPIED)

    def test_download_before_completion_is_an_error(self):
        response = self.request.download_signed(None)
        self.assertEqual(response.status_code, 400)


class ApprovalTests(SimpleTestCase):
    def test_actions_that_email_signers_or_void_need_approval(self):
        self.assertTrue(requires_approval(SignatureRequest, "resend_action"))
        self.assertTrue(requires_approval(SignatureRequest, "void_action"))
        self.assertFalse(requires_approval(SignatureRequest, "preview_source"))

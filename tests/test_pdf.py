import io

from django.test import SimpleTestCase
from pypdf import PdfReader

from crudkit_sign import pdf
from crudkit_sign.signable import CHECKBOX, DATE, NAME, SIGNATURE, TEXT
from tests.helpers import png_bytes
from tests.testapp.models import build_pdf


class FindAnchorsTests(SimpleTestCase):
    def test_finds_every_anchor_with_kind_role_and_name(self):
        source = build_pdf(
            ["{{sig:customer}}", "{{date:customer}}", "{{text:customer:po_number}}", "{{check:company}}"], pages=2
        )
        specs = pdf.find_anchors(source)
        by_anchor = {spec.anchor: spec for spec in specs}
        self.assertEqual(
            set(by_anchor),
            {"{{sig:customer}}", "{{date:customer}}", "{{text:customer:po_number}}", "{{check:company}}"},
        )
        self.assertEqual(by_anchor["{{sig:customer}}"].kind, SIGNATURE)
        self.assertEqual(by_anchor["{{sig:customer}}"].role, "customer")
        self.assertEqual(by_anchor["{{date:customer}}"].kind, DATE)
        self.assertEqual(by_anchor["{{text:customer:po_number}}"].kind, TEXT)
        self.assertEqual(by_anchor["{{text:customer:po_number}}"].name, "po_number")
        self.assertEqual(by_anchor["{{check:company}}"].kind, CHECKBOX)
        self.assertEqual(by_anchor["{{check:company}}"].role, "company")
        # Anchors were drawn on the last page, at x=72 and descending y from 200.
        for spec in specs:
            self.assertEqual(spec.page, 2)
            self.assertAlmostEqual(spec.x, 72, delta=1)
            self.assertLess(spec.y, 205)
            self.assertGreater(spec.y, 60)
        self.assertEqual(by_anchor["{{sig:customer}}"].width, pdf.DEFAULT_SIZES[SIGNATURE][0])

    def test_no_anchors(self):
        self.assertEqual(pdf.find_anchors(build_pdf([])), [])

    def test_anchor_text_round_trip(self):
        self.assertEqual(pdf.anchor_text(SIGNATURE, "customer"), "{{sig:customer}}")
        self.assertEqual(pdf.anchor_text(TEXT, "customer", "po"), "{{text:customer:po}}")
        self.assertEqual(pdf.anchor_text(NAME, "company"), "{{name:company}}")


class StampTests(SimpleTestCase):
    def test_stamp_keeps_pages_and_appends_certificate(self):
        source = build_pdf(["{{sig:customer}}"], pages=2)
        placements = [
            pdf.Placement(page=2, x=72, y=200, width=180, height=50, kind=SIGNATURE, image=png_bytes()),
            pdf.Placement(page=2, x=72, y=160, width=100, height=16, kind=DATE, value="2026-09-19"),
            pdf.Placement(page=1, x=72, y=100, width=14, height=14, kind=CHECKBOX, value="true"),
        ]
        certificate = pdf.certificate_page(
            {
                "title": "Order form",
                "request_id": "SRQ1",
                "brand_name": "Example",
                "created_at": "2026-09-19 10:00:00 UTC",
                "completed_at": "2026-09-19 11:00:00 UTC",
                "source_sha256": "a" * 64,
                "signed_sha256": "b" * 64,
                "signers": [
                    {
                        "name": "Ada",
                        "email": "ada@example.com",
                        "role": "customer",
                        "status": "Signed",
                        "viewed_at": "",
                        "signed_at": "2026-09-19 11:00:00 UTC",
                        "ip_address": "10.0.0.1",
                        "consent_accepted_at": "2026-09-19 11:00:00 UTC",
                    }
                ],
                "events": [
                    {"at": "2026-09-19 11:00:00 UTC", "kind": "Signed", "who": "Ada", "ip_address": "", "detail": ""}
                ],
            }
        )
        signed = pdf.stamp(source, placements, certificate)
        reader = PdfReader(io.BytesIO(signed))
        self.assertEqual(len(reader.pages), 3)
        self.assertIn("2026-09-19", reader.pages[1].extract_text())
        self.assertIn("Certificate of completion", reader.pages[2].extract_text())
        self.assertIn("b" * 64, reader.pages[2].extract_text())
        # The stamped page carries the signature image.
        self.assertTrue(reader.pages[1].images)
        self.assertNotEqual(pdf.sha256(signed), pdf.sha256(source))

    def test_page_with_only_blank_fields_is_kept(self):
        source = build_pdf(["{{sig:customer}}"], pages=2)
        placements = [
            pdf.Placement(page=1, x=72, y=100, width=14, height=14, kind=CHECKBOX, value=""),
            pdf.Placement(page=1, x=72, y=80, width=180, height=16, kind=TEXT, value=""),
            pdf.Placement(page=2, x=72, y=160, width=100, height=16, kind=DATE, value="2026-09-30"),
        ]
        reader = PdfReader(io.BytesIO(pdf.stamp(source, placements)))
        self.assertEqual(len(reader.pages), 2)
        self.assertIn("2026-09-30", reader.pages[1].extract_text())

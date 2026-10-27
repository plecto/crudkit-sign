"""PDF work: find anchor fields, stamp signatures onto pages, build the certificate page.

Coordinates are PDF points with the origin at the bottom-left of the page, the
same convention as reportlab and pdf.js.
"""

import hashlib
import io
from dataclasses import dataclass
from xml.sax.saxutils import escape

import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from crudkit_sign.signable import CHECKBOX, DATE, INITIALS, NAME, SIGNATURE, TEXT, FieldSpec

# {{sig:customer}}, {{date:customer}}, {{text:customer:po_number}} ...
ANCHOR_PATTERN = r"\{\{(sig|initials|date|name|text|check):([A-Za-z0-9_-]+)(?::([A-Za-z0-9_-]+))?\}\}"
ANCHOR_KINDS = {
    "sig": SIGNATURE,
    "initials": INITIALS,
    "date": DATE,
    "name": NAME,
    "text": TEXT,
    "check": CHECKBOX,
}
# Width and height in points of the box placed at an anchor, per kind.
DEFAULT_SIZES = {
    SIGNATURE: (180, 50),
    INITIALS: (60, 30),
    DATE: (100, 16),
    NAME: (180, 16),
    TEXT: (180, 16),
    CHECKBOX: (14, 14),
}
TEXT_FONT = "Helvetica"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def anchor_text(kind, role, name=""):
    prefix = next(prefix for prefix, kind_ in ANCHOR_KINDS.items() if kind_ == kind)
    return "{{" + ":".join(part for part in (prefix, role, name) if part) + "}}"


def find_anchors(pdf_bytes):
    """Every anchor string in the PDF as a FieldSpec placed at the anchor's position."""
    specs = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            for match in page.search(ANCHOR_PATTERN, regex=True, case=True, return_groups=True, return_chars=False):
                prefix, role, name = match["groups"]
                kind = ANCHOR_KINDS[prefix]
                width, height = DEFAULT_SIZES[kind]
                specs.append(
                    FieldSpec(
                        role=role,
                        kind=kind,
                        page=page_number,
                        x=float(match["x0"]),
                        y=float(page.height - match["bottom"]),
                        width=width,
                        height=height,
                        name=name or "",
                        anchor=match["text"],
                    )
                )
    return specs


def page_count(pdf_bytes):
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


@dataclass(frozen=True)
class Placement:
    page: int
    x: float
    y: float
    width: float
    height: float
    kind: str
    value: str = ""
    image: bytes | None = None


def _draw(pdf_canvas, placement):
    if placement.kind in (SIGNATURE, INITIALS):
        if placement.image:
            pdf_canvas.drawImage(
                ImageReader(io.BytesIO(placement.image)),
                placement.x,
                placement.y,
                placement.width,
                placement.height,
                preserveAspectRatio=True,
                anchor="sw",
                mask="auto",
            )
    elif placement.kind == CHECKBOX:
        if placement.value:
            size = max(placement.height - 2, 6)
            pdf_canvas.setFont("Helvetica-Bold", size)
            # Helvetica's cap height is about 0.72em; this centres the X in the box.
            pdf_canvas.drawCentredString(
                placement.x + placement.width / 2, placement.y + (placement.height - 0.72 * size) / 2, "X"
            )
    elif placement.value:
        size = min(10, max(placement.height * 0.7, 6))
        pdf_canvas.setFont(TEXT_FONT, size)
        pdf_canvas.drawString(placement.x + 2, placement.y + (placement.height - size) / 2 + 1, placement.value)


def stamp(pdf_bytes, placements, certificate_pdf=None):
    """Overlay placements on their pages and append the certificate; returns the new PDF."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    by_page = {}
    for placement in placements:
        by_page.setdefault(placement.page, []).append(placement)
    for page_number, page in enumerate(reader.pages, start=1):
        items = by_page.get(page_number)
        if items:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            buffer = io.BytesIO()
            pdf_canvas = canvas.Canvas(buffer, pagesize=(width, height))
            for placement in items:
                _draw(pdf_canvas, placement)
            # save() drops a page nothing was drawn on, e.g. when every field on it was left blank.
            pdf_canvas.showPage()
            pdf_canvas.save()
            page.merge_page(PdfReader(buffer).pages[0])
        writer.add_page(page)
    if certificate_pdf:
        for page in PdfReader(io.BytesIO(certificate_pdf)).pages:
            writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _cell(text, style):
    return Paragraph(escape(str(text if text is not None else "")), style)


def certificate_page(data):
    """The certificate of completion as a PDF.

    ``data`` has: title, request_id, brand_name, created_at, completed_at,
    source_sha256, signed_sha256, signers (list of dicts) and events (list of dicts).
    """
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    body.fontSize = 8
    body.leading = 10
    small = styles["BodyText"].clone("small", fontSize=7, leading=9)
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm
    )
    grid = TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
    )
    story = [
        Paragraph("Certificate of completion", styles["Title"]),
        Paragraph(escape(data["title"]), styles["Heading2"]),
        Spacer(1, 4 * mm),
    ]
    summary = [
        ["Request", data["request_id"]],
        ["Issued by", data["brand_name"]],
        ["Created", data["created_at"]],
        ["Completed", data["completed_at"]],
        ["Original document SHA-256", data["source_sha256"]],
        ["Signed document SHA-256", data["signed_sha256"]],
    ]
    summary_table = Table([[_cell(k, body), _cell(v, body)] for k, v in summary], colWidths=[45 * mm, 125 * mm])
    summary_table.setStyle(
        TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")])
    )
    story += [summary_table, Spacer(1, 6 * mm), Paragraph("Signers", styles["Heading3"])]
    signer_rows = [["Name", "Email", "Role", "Status", "Viewed", "Signed", "IP address", "Consent"]]
    for signer in data["signers"]:
        signer_rows.append(
            [
                signer["name"],
                signer["email"],
                signer["role"],
                signer["status"],
                signer["viewed_at"],
                signer["signed_at"],
                signer["ip_address"],
                signer["consent_accepted_at"],
            ]
        )
    signer_table = Table(
        [[_cell(value, small) for value in row] for row in signer_rows],
        colWidths=[26 * mm, 34 * mm, 16 * mm, 16 * mm, 20 * mm, 20 * mm, 20 * mm, 20 * mm],
        repeatRows=1,
    )
    signer_table.setStyle(grid)
    story += [signer_table, Spacer(1, 6 * mm), Paragraph("Audit trail", styles["Heading3"])]
    event_rows = [["Time", "Event", "Who", "IP address", "Detail"]]
    for event in data["events"]:
        event_rows.append([event["at"], event["kind"], event["who"], event["ip_address"], event["detail"]])
    event_table = Table(
        [[_cell(value, small) for value in row] for row in event_rows],
        colWidths=[32 * mm, 22 * mm, 40 * mm, 26 * mm, 50 * mm],
        repeatRows=1,
    )
    event_table.setStyle(grid)
    story += [
        event_table,
        Spacer(1, 6 * mm),
        Paragraph(
            "The signed document is the pages preceding this certificate. Its SHA-256 hash above was "
            "computed over those pages before this certificate was appended.",
            small,
        ),
    ]
    document.build(story)
    return buffer.getvalue()

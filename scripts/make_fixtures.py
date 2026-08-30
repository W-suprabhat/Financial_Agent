"""
Regenerate the synthetic test PDFs in tests/fixtures.

    python -m scripts.make_fixtures

Produces two documents that exercise different extraction paths:

  hard_statement.pdf     text-layer PDF; parenthetical negatives, two comparative
                         year columns (a trap: the current year must win), GBP
                         millions, footnote references, subtotals
  scanned_statement.pdf  image-only PDF with NO text layer, slightly rotated;
                         only readable if the model's vision path is working

Requires the dev extras: pip install -r requirements-dev.txt
"""

import sys
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)


def build_text_pdf() -> Path:
    path = OUT / "hard_statement.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    W, H = letter

    c.setFont("Helvetica-Bold", 13)
    c.drawString(60, H - 60, "GLOBEX INDUSTRIES PLC")
    c.setFont("Helvetica", 9)
    c.drawString(60, H - 74, "Consolidated Financial Statements (unaudited)")
    c.drawString(60, H - 86, "Amounts in GBP millions, except per-share data")
    c.drawString(60, H - 98, "Year ended 31 December 2024  (prior year comparatives shown)")

    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(420, H - 118, "FY2024")
    c.drawRightString(500, H - 118, "FY2023")
    c.line(60, H - 122, 500, H - 122)

    rows = [
        ("INCOME STATEMENT", None, None, True),
        ("Turnover", "4,812.6", "4,201.3", False),
        ("Cost of sales", "(2,918.4)", "(2,610.7)", False),
        ("Gross profit", "1,894.2", "1,590.6", False),
        ("Administrative expenses (note 4)", "(742.9)", "(701.2)", False),
        ("Operating profit", "1,151.3", "889.4", False),
        ("Finance costs", "(88.7)", "(94.1)", False),
        ("Taxation", "(265.4)", "(198.3)", False),
        ("Profit for the year", "797.2", "597.0", False),
        ("", None, None, False),
        ("BALANCE SHEET", None, None, True),
        ("Non-current assets", "6,204.1", "5,880.9", False),
        ("Inventories", "812.3", "764.5", False),
        ("Trade and other receivables", "1,043.7", "961.2", False),
        ("Cash and cash equivalents", "489.5", "402.8", False),
        ("Total current assets", "2,345.5", "2,128.5", False),
        ("Total assets", "8,549.6", "8,009.4", False),
        ("Trade and other payables", "(1,102.4)", "(1,048.9)", False),
        ("Borrowings due within one year", "(320.0)", "(280.0)", False),
        ("Total current liabilities", "(1,422.4)", "(1,328.9)", False),
        ("Borrowings due after one year", "(2,410.0)", "(2,560.0)", False),
        ("Total liabilities", "(3,832.4)", "(3,888.9)", False),
        ("Net assets / Total equity", "4,717.2", "4,120.5", False),
        ("", None, None, False),
        ("CASH FLOW STATEMENT", None, None, True),
        ("Net cash from operating activities", "1,288.9", "1,004.2", False),
        ("Net cash used in investing activities", "(690.3)", "(612.7)", False),
        ("Net cash used in financing activities", "(512.9)", "(430.1)", False),
        ("Net increase in cash", "85.7", "(38.6)", False),
    ]

    y = H - 140
    for label, cur, prior, is_header in rows:
        if is_header:
            c.setFont("Helvetica-Bold", 10)
            c.drawString(60, y, label)
            y -= 15
            continue
        if not label:
            y -= 8
            continue
        c.setFont("Helvetica", 9)
        c.drawString(70, y, label)
        if cur:
            c.drawRightString(420, y, cur)
        if prior:
            c.drawRightString(500, y, prior)
        y -= 13

    c.setFont("Helvetica-Oblique", 7)
    c.drawString(60, 70, "Note 4: Administrative expenses include a one-off restructuring charge of 41.2.")
    c.drawString(60, 60, "Figures in parentheses denote negative amounts / cash outflows.")
    c.save()
    return path


def build_scanned_pdf() -> Path:
    from PIL import Image, ImageDraw, ImageFont

    path = OUT / "scanned_statement.pdf"
    W, H = 1700, 2200
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    try:
        big = ImageFont.truetype("arialbd.ttf", 46)
        mid = ImageFont.truetype("arial.ttf", 36)
    except Exception:
        big = mid = ImageFont.load_default()

    d.text((90, 90), "NORTHWIND TRADING CO.", font=big, fill="black")
    d.text((90, 155), "Statement of Operations - FY2025", font=mid, fill="black")
    d.text((90, 205), "USD thousands", font=mid, fill="black")

    rows = [
        ("Net sales", "2,455"), ("Cost of revenue", "(1,180)"),
        ("Gross profit", "1,275"), ("Operating expenses", "(610)"),
        ("Operating income", "665"), ("Interest expense", "(45)"),
        ("Income tax", "(155)"), ("Net income", "465"),
    ]
    y = 300
    for label, val in rows:
        d.text((110, y), label, font=mid, fill="black")
        d.text((1150, y), val, font=mid, fill="black")
        y += 62

    # A slight rotation so it reads like a real scan
    img = img.rotate(-0.7, expand=False, fillcolor="white")

    tmp_png = OUT / "_scan_page.png"
    img.save(tmp_png)

    c = canvas.Canvas(str(path), pagesize=letter)
    pw, ph = letter
    c.drawImage(ImageReader(str(tmp_png)), 0, 0, width=pw, height=ph)
    c.save()
    tmp_png.unlink(missing_ok=True)
    return path


def main() -> int:
    text_pdf = build_text_pdf()
    print(f"wrote {text_pdf.relative_to(OUT.parent.parent)}")

    try:
        scan_pdf = build_scanned_pdf()
    except ImportError:
        print("skipped scanned_statement.pdf (pillow not installed)", file=sys.stderr)
        return 0

    # Assert the fixture is genuinely image-only, otherwise it doesn't test vision
    try:
        from pypdf import PdfReader
        text = "".join((p.extract_text() or "") for p in PdfReader(str(scan_pdf)).pages)
        if text.strip():
            print(f"WARNING: {scan_pdf.name} has a text layer; it will not test the "
                  "vision path", file=sys.stderr)
        else:
            print(f"wrote {scan_pdf.relative_to(OUT.parent.parent)} (verified: no text layer)")
    except ImportError:
        print(f"wrote {scan_pdf.relative_to(OUT.parent.parent)} (install pypdf to verify)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

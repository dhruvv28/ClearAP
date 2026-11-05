"""Generate realistic test invoice PDFs in five different vendor layouts.

Run:  python scripts/generate_invoices.py
Writes PDFs to samples/ plus samples/manifest.json describing each scenario.
"""
import io
import json
import random
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"
OUT.mkdir(exist_ok=True)

pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))

W, H = A4
BUYER = {
    "name": "Meridian Retail Pvt Ltd",
    "addr": ["No. 14, 3rd Floor, Prestige Meridian", "MG Road, Bengaluru 560001, Karnataka"],
    "gstin": "29AAACM1234F1ZU",
}


def inr(x: float) -> str:
    """Indian digit grouping: 1234567.5 -> 12,34,567.50"""
    neg = x < 0
    x = abs(x)
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return ("-" if neg else "") + whole + "." + frac


ONES = "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen".split()
TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def words(n: int) -> str:
    def two(n):
        return ONES[n] if n < 20 else TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])

    def three(n):
        h, r = divmod(n, 100)
        s = (ONES[h] + " Hundred") if h else ""
        if r:
            s += (" " if s else "") + two(r)
        return s

    parts = []
    for div, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        q, n = divmod(n, div)
        if q:
            parts.append(three(q) + " " + name)
    if n:
        parts.append(three(n))
    return " ".join(parts) or "Zero"


# --------------------------------------------------------------------------
# Template A — Kaveri: classic bordered GST invoice with HSN column
# --------------------------------------------------------------------------
def template_kaveri(c: canvas.Canvas, d: dict):
    c.setFont("Helvetica-Bold", 15)
    c.drawString(18 * mm, H - 22 * mm, "Kaveri Office Supplies Pvt Ltd")
    c.setFont("Helvetica", 8.5)
    y = H - 27 * mm
    for line in ["#22, 1st Cross, Lalbagh Road, Bengaluru 560027", "Ph: +91 80 4123 7788 | accounts@kaverioffice.in",
                 "GSTIN: 29AAGCK4512M1Z8"]:
        c.drawString(18 * mm, y, line)
        y -= 4.2 * mm
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(W - 18 * mm, H - 22 * mm, "TAX INVOICE")
    c.setFont("Helvetica", 9)
    c.drawRightString(W - 18 * mm, H - 27 * mm, "Original for Recipient")

    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    top = H - 44 * mm
    c.rect(18 * mm, top - 30 * mm, W - 36 * mm, 30 * mm)
    c.line(W / 2, top, W / 2, top - 30 * mm)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(21 * mm, top - 5 * mm, "Bill To:")
    c.setFont("Helvetica", 9)
    yy = top - 10 * mm
    for line in [BUYER["name"]] + BUYER["addr"] + ["GSTIN: " + BUYER["gstin"]]:
        c.drawString(21 * mm, yy, line)
        yy -= 4.3 * mm
    yy = top - 6 * mm
    for k, v in [("Invoice No:", d["invoice_number"]), ("Invoice Date:", d["date_str"]),
                 ("PO No:", d.get("po", "")), ("Place of Supply:", "Karnataka (29)")]:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(W / 2 + 4 * mm, yy, k)
        c.setFont("Helvetica", 9)
        c.drawString(W / 2 + 34 * mm, yy, v)
        yy -= 5.5 * mm

    # line items
    ty = top - 38 * mm
    cols = [18, 28, 105, 125, 142, 165, 192]
    c.setFillColor(colors.HexColor("#e8e8e8"))
    c.rect(18 * mm, ty - 2 * mm, (192 - 18) * mm, 7 * mm, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 8.5)
    heads = ["S.No", "Description of Goods", "HSN", "Qty", "Rate", "Amount"]
    for i, h in enumerate(heads):
        if i >= 3:
            c.drawRightString((cols[i + 1] - 2) * mm, ty, h)
        else:
            c.drawString((cols[i] + 2) * mm, ty, h)
    c.setFont("Helvetica", 9)
    y = ty - 8 * mm
    for i, li in enumerate(d["lines"], 1):
        c.drawString(20 * mm, y, str(i))
        c.drawString(30 * mm, y, li["desc"])
        c.drawString(107 * mm, y, li.get("hsn", ""))
        c.drawRightString(140 * mm, y, str(li["qty"]))
        c.drawRightString(163 * mm, y, inr(li["rate"]))
        c.drawRightString(190 * mm, y, inr(li["qty"] * li["rate"]))
        y -= 7 * mm
    c.line(18 * mm, y + 3 * mm, 192 * mm, y + 3 * mm)
    sub = d["subtotal"]
    tax = d["tax"]
    y -= 3 * mm
    for k, v in [("Sub Total", sub), ("CGST @ 9%", tax / 2), ("SGST @ 9%", tax / 2)]:
        c.drawRightString(163 * mm, y, k)
        c.drawRightString(190 * mm, y, inr(v))
        y -= 6 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(163 * mm, y, "Grand Total (INR)")
    c.drawRightString(190 * mm, y, inr(sub + tax))
    y -= 9 * mm
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(20 * mm, y, f"Amount in words: Rupees {words(int(round(sub + tax)))} Only")
    y -= 14 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y, "Bank Details")
    c.setFont("Helvetica", 9)
    for line in [f"Bank: {d['bank'][0]}", f"A/c No: {d['bank'][1]}", f"IFSC: {d['bank'][2]}"]:
        y -= 4.8 * mm
        c.drawString(20 * mm, y, line)
    c.drawRightString(190 * mm, y, "Authorised Signatory")
    c.setFont("Helvetica", 7.5)
    c.drawString(20 * mm, 15 * mm, "Payment terms: Net 30 days. Subject to Bengaluru jurisdiction.")


# --------------------------------------------------------------------------
# Template B — Nimbus: modern SaaS invoice, rupee symbol, IGST
# --------------------------------------------------------------------------
def template_nimbus(c: canvas.Canvas, d: dict):
    accent = colors.HexColor("#5b3cc4")
    c.setFillColor(accent)
    c.rect(0, H - 6 * mm, W, 6 * mm, fill=1, stroke=0)
    c.setFont("DejaVu-Bold", 20)
    c.drawString(20 * mm, H - 24 * mm, "nimbus")
    c.setFillColor(colors.HexColor("#333333"))
    c.setFont("DejaVu", 8)
    c.drawString(20 * mm, H - 29 * mm, "Nimbus Cloud Services LLP")
    c.drawString(20 * mm, H - 33 * mm, "Baner Road, Pune 411045, Maharashtra")
    c.drawString(20 * mm, H - 37 * mm, "GSTIN 27AAJFN7781K1ZJ")
    c.setFont("DejaVu-Bold", 22)
    c.setFillColor(colors.HexColor("#111111"))
    c.drawRightString(W - 20 * mm, H - 24 * mm, "Invoice")
    c.setFont("DejaVu", 9)
    y = H - 32 * mm
    meta = []
    if d.get("invoice_number"):
        meta.append(("Invoice #", d["invoice_number"]))
    if d.get("date_str"):
        meta.append(("Issue date", d["date_str"]))
        meta.append(("Due date", d["due_str"]))
    meta.append(("Your PO", d.get("po", "")))
    for k, v in meta:
        c.setFillColor(colors.HexColor("#777777"))
        c.drawRightString(W - 52 * mm, y, k)
        c.setFillColor(colors.HexColor("#111111"))
        c.drawRightString(W - 20 * mm, y, v)
        y -= 5 * mm

    y = H - 62 * mm
    c.setFont("DejaVu-Bold", 8)
    c.setFillColor(colors.HexColor("#777777"))
    c.drawString(20 * mm, y, "BILLED TO")
    c.setFont("DejaVu", 9)
    c.setFillColor(colors.HexColor("#111111"))
    for line in [BUYER["name"]] + BUYER["addr"] + ["GSTIN " + BUYER["gstin"]]:
        y -= 4.6 * mm
        c.drawString(20 * mm, y, line)

    y -= 14 * mm
    c.setFont("DejaVu-Bold", 8.5)
    c.setFillColor(colors.HexColor("#777777"))
    c.drawString(20 * mm, y, "Description")
    c.drawRightString(130 * mm, y, "Qty")
    c.drawRightString(158 * mm, y, "Unit price")
    c.drawRightString(W - 20 * mm, y, "Amount")
    c.setStrokeColor(colors.HexColor("#dddddd"))
    c.line(20 * mm, y - 2.5 * mm, W - 20 * mm, y - 2.5 * mm)
    c.setFont("DejaVu", 9.5)
    c.setFillColor(colors.HexColor("#111111"))
    y -= 9 * mm
    for li in d["lines"]:
        c.drawString(20 * mm, y, li["desc"])
        c.drawRightString(130 * mm, y, str(li["qty"]))
        c.drawRightString(158 * mm, y, "₹" + inr(li["rate"]))
        c.drawRightString(W - 20 * mm, y, "₹" + inr(li["qty"] * li["rate"]))
        y -= 8 * mm
    c.line(110 * mm, y + 3 * mm, W - 20 * mm, y + 3 * mm)
    y -= 3 * mm
    sub, tax = d["subtotal"], d["tax"]
    for k, v in [("Subtotal", sub), ("IGST 18%", tax)]:
        c.drawRightString(158 * mm, y, k)
        c.drawRightString(W - 20 * mm, y, "₹" + inr(v))
        y -= 6 * mm
    c.setFont("DejaVu-Bold", 11)
    c.drawRightString(158 * mm, y - 1 * mm, "Amount due")
    c.setFillColor(accent)
    c.drawRightString(W - 20 * mm, y - 1 * mm, "₹" + inr(sub + tax))
    c.setFillColor(colors.HexColor("#555555"))
    c.setFont("DejaVu", 8.5)
    y -= 20 * mm
    c.drawString(20 * mm, y, "Pay by bank transfer")
    for line in [f"Bank: {d['bank'][0]}", f"Account Number: {d['bank'][1]}", f"IFSC: {d['bank'][2]}"]:
        y -= 4.5 * mm
        c.drawString(20 * mm, y, line)
    c.setFont("DejaVu", 7.5)
    c.drawString(20 * mm, 14 * mm, "Questions? billing@nimbuscloud.in  ·  Inter-state supply, IGST applicable.")


# --------------------------------------------------------------------------
# Template C — Deccan: logistics bill, bundled freight line, IGST 12%
# --------------------------------------------------------------------------
def template_deccan(c: canvas.Canvas, d: dict):
    c.setFont("Times-Bold", 16)
    c.drawCentredString(W / 2, H - 20 * mm, "DECCAN LOGISTICS & FREIGHT PVT LTD")
    c.setFont("Times-Roman", 9)
    c.drawCentredString(W / 2, H - 25 * mm, "Plot 7, Shamshabad Logistics Park, Hyderabad 501218, Telangana")
    c.drawCentredString(W / 2, H - 29 * mm, "GSTIN: 36AADCD3345P1ZC   |   PAN: AADCD3345P")
    c.setLineWidth(1.2)
    c.line(15 * mm, H - 32 * mm, W - 15 * mm, H - 32 * mm)
    c.setFont("Times-Bold", 12)
    c.drawCentredString(W / 2, H - 39 * mm, "TAX INVOICE  (Goods Transport Agency)")
    c.setFont("Times-Roman", 10)
    y = H - 49 * mm
    left = [("Bill No:", d["invoice_number"]), ("Dated:", d["date_str"]), ("Customer Order Ref:", d.get("po", ""))]
    right = [("Consignee:", BUYER["name"]), ("", "Bengaluru DC, Hoskote"), ("Recipient GSTIN:", BUYER["gstin"])]
    for (k1, v1), (k2, v2) in zip(left, right):
        c.setFont("Times-Bold", 10); c.drawString(15 * mm, y, k1)
        c.setFont("Times-Roman", 10); c.drawString(52 * mm, y, v1)
        c.setFont("Times-Bold", 10); c.drawString(110 * mm, y, k2)
        c.setFont("Times-Roman", 10); c.drawString(140 * mm, y, v2)
        y -= 6 * mm
    y -= 6 * mm
    c.setLineWidth(0.6)
    c.rect(15 * mm, y - 2.5 * mm, W - 30 * mm, 8 * mm)
    c.setFont("Times-Bold", 10)
    c.drawString(17 * mm, y, "Particulars")
    c.drawRightString(135 * mm, y, "Qty")
    c.drawRightString(162 * mm, y, "Rate")
    c.drawRightString(W - 17 * mm, y, "Amount (Rs.)")
    c.setFont("Times-Roman", 10)
    y -= 10 * mm
    for li in d["lines"]:
        c.drawString(17 * mm, y, li["desc"])
        c.drawRightString(135 * mm, y, f"{li['qty']} Lot")
        c.drawRightString(162 * mm, y, inr(li["rate"]))
        c.drawRightString(W - 17 * mm, y, inr(li["qty"] * li["rate"]))
        y -= 6 * mm
        c.setFont("Times-Italic", 8.5)
        c.drawString(20 * mm, y, li.get("note", ""))
        c.setFont("Times-Roman", 10)
        y -= 8 * mm
    c.line(15 * mm, y + 2 * mm, W - 15 * mm, y + 2 * mm)
    y -= 4 * mm
    sub, tax = d["subtotal"], d["tax"]
    for k, v in [("Taxable Value", sub), ("IGST @ 12%", tax)]:
        c.drawRightString(162 * mm, y, k); c.drawRightString(W - 17 * mm, y, inr(v)); y -= 6 * mm
    c.setFont("Times-Bold", 11)
    c.drawRightString(162 * mm, y, "Total Amount Payable")
    c.drawRightString(W - 17 * mm, y, inr(sub + tax))
    y -= 9 * mm
    c.setFont("Times-Italic", 9)
    c.drawString(17 * mm, y, f"Rupees {words(int(round(sub + tax)))} Only")
    y -= 14 * mm
    c.setFont("Times-Roman", 9.5)
    c.drawString(17 * mm, y, f"Remit to: {d['bank'][0]}, Bank A/C {d['bank'][1]}, IFSC {d['bank'][2]}")
    y -= 5 * mm
    c.drawString(17 * mm, y, "Shipment-wise annexure attached separately. E. & O.E.")
    c.drawRightString(W - 17 * mm, 22 * mm, "For Deccan Logistics & Freight Pvt Ltd")


# --------------------------------------------------------------------------
# Template D — Prism: dark header bar, IGST, optional bank-change notice
# --------------------------------------------------------------------------
def template_prism(c: canvas.Canvas, d: dict):
    navy = colors.HexColor("#14213d")
    c.setFillColor(navy)
    c.rect(0, H - 34 * mm, W, 34 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(16 * mm, H - 16 * mm, "PRISM IT HARDWARE SOLUTIONS")
    c.setFont("Helvetica", 8.5)
    c.drawString(16 * mm, H - 22 * mm, "Prism IT Hardware Solutions Pvt Ltd | 41 Anna Salai, Chennai 600002, Tamil Nadu")
    c.drawString(16 * mm, H - 27 * mm, "GSTIN 33AAFCP6612H1ZL | sales@prismit.co.in")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 16 * mm, H - 16 * mm, "TAX INVOICE")
    c.setFillColor(colors.black)
    y = H - 46 * mm
    c.setFont("Helvetica", 9.5)
    for k, v in [("Tax Invoice No", d["invoice_number"]), ("Invoice Date", d["date_str"]), ("P.O. Number", d.get("po", ""))]:
        c.setFont("Helvetica-Bold", 9.5); c.drawString(16 * mm, y, k + " :")
        c.setFont("Helvetica", 9.5); c.drawString(50 * mm, y, v)
        y -= 5.5 * mm
    yy = H - 46 * mm
    c.setFont("Helvetica-Bold", 9.5); c.drawString(120 * mm, yy, "Ship / Bill To")
    c.setFont("Helvetica", 9)
    for line in [BUYER["name"]] + BUYER["addr"] + ["GSTIN " + BUYER["gstin"]]:
        yy -= 4.6 * mm
        c.drawString(120 * mm, yy, line)

    y = H - 80 * mm
    c.setFillColor(colors.HexColor("#f0f2f7"))
    c.rect(16 * mm, y - 2.5 * mm, W - 32 * mm, 8 * mm, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18 * mm, y, "Item")
    c.drawRightString(132 * mm, y, "Qty")
    c.drawRightString(160 * mm, y, "Unit Price")
    c.drawRightString(W - 18 * mm, y, "Total")
    c.setFont("Helvetica", 9.5)
    y -= 10 * mm
    for li in d["lines"]:
        c.drawString(18 * mm, y, li["desc"])
        c.drawRightString(132 * mm, y, str(li["qty"]))
        c.drawRightString(160 * mm, y, inr(li["rate"]))
        c.drawRightString(W - 18 * mm, y, inr(li["qty"] * li["rate"]))
        y -= 7.5 * mm
    c.setStrokeColor(colors.HexColor("#cccccc"))
    c.line(16 * mm, y + 3 * mm, W - 16 * mm, y + 3 * mm)
    y -= 3 * mm
    sub, tax = d["subtotal"], d["tax"]
    for k, v in [("Taxable Amount", sub), ("IGST @ 18%", tax)]:
        c.drawRightString(160 * mm, y, k); c.drawRightString(W - 18 * mm, y, inr(v)); y -= 6 * mm
    c.setFont("Helvetica-Bold", 10.5)
    c.drawRightString(160 * mm, y, "Invoice Total INR")
    c.drawRightString(W - 18 * mm, y, inr(sub + tax))
    y -= 16 * mm
    if d.get("bank_notice"):
        c.setFillColor(colors.HexColor("#fff4d6"))
        c.rect(16 * mm, y - 9 * mm, W - 32 * mm, 14 * mm, fill=1, stroke=0)
        c.setFillColor(colors.HexColor("#7a4b00"))
        c.setFont("Helvetica-Bold", 9)
        c.drawString(19 * mm, y, "IMPORTANT: Our banking details have changed with effect from 15 Oct 2026.")
        c.setFont("Helvetica", 8.5)
        c.drawString(19 * mm, y - 5 * mm, "Kindly update your vendor records and remit this and all future payments to the account below.")
        c.setFillColor(colors.black)
        y -= 18 * mm
    c.setFont("Helvetica-Bold", 9); c.drawString(18 * mm, y, "Remittance Details")
    c.setFont("Helvetica", 9)
    for line in [f"Bank Name: {d['bank'][0]}", f"Account Number: {d['bank'][1]}", f"IFSC Code: {d['bank'][2]}"]:
        y -= 4.8 * mm
        c.drawString(18 * mm, y, line)
    c.setFont("Helvetica", 7.5)
    c.drawString(16 * mm, 14 * mm, "Payment terms: 45 days from invoice date. Goods once sold will not be taken back.")


# --------------------------------------------------------------------------
# Template E — plain letterhead, tax-inclusive pricing (Sahyadri / Apex)
# --------------------------------------------------------------------------
def template_plain(c: canvas.Canvas, d: dict):
    v = d["vendor"]
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, H - 22 * mm, v["name"])
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, H - 27 * mm, v["addr"])
    c.drawString(20 * mm, H - 31 * mm, "GSTIN: " + v["gstin"])
    c.line(20 * mm, H - 35 * mm, W - 20 * mm, H - 35 * mm)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, H - 45 * mm, "INVOICE")
    c.setFont("Helvetica", 10)
    y = H - 54 * mm
    for k, val in [("Invoice Number", d["invoice_number"]), ("Date", d["date_str"])] + (
            [("Purchase Order", d["po"])] if d.get("po") else []):
        c.drawString(20 * mm, y, f"{k}: {val}")
        y -= 5.5 * mm
    y -= 4 * mm
    c.drawString(20 * mm, y, "To,")
    for line in [BUYER["name"]] + BUYER["addr"] + ["GSTIN: " + BUYER["gstin"]]:
        y -= 5 * mm
        c.drawString(20 * mm, y, line)
    y -= 12 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20 * mm, y, "Description")
    c.drawRightString(135 * mm, y, "Qty")
    c.drawRightString(160 * mm, y, "Rate")
    c.drawRightString(W - 20 * mm, y, "Amount")
    c.setFont("Helvetica", 10)
    y -= 8 * mm
    for li in d["lines"]:
        c.drawString(20 * mm, y, li["desc"])
        c.drawRightString(135 * mm, y, str(li["qty"]))
        c.drawRightString(160 * mm, y, inr(li["rate"]))
        c.drawRightString(W - 20 * mm, y, inr(li["qty"] * li["rate"]))
        y -= 7 * mm
    c.line(20 * mm, y + 3 * mm, W - 20 * mm, y + 3 * mm)
    y -= 4 * mm
    if d.get("inclusive"):
        total = sum(li["qty"] * li["rate"] for li in d["lines"])
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(20 * mm, y, "Total Amount (inclusive of GST @ 18%)")
        c.drawRightString(W - 20 * mm, y, inr(total))
        y -= 6 * mm
        c.setFont("Helvetica", 9)
        half = round(total - total / 1.18, 2) / 2
        c.drawString(20 * mm, y, f"Tax component included above: CGST 9% {inr(half)} + SGST 9% {inr(half)}")
    else:
        sub, tax = d["subtotal"], d["tax"]
        for k, val in [("Sub Total", sub), ("IGST @ 18%", tax)]:
            c.drawString(110 * mm, y, k); c.drawRightString(W - 20 * mm, y, inr(val)); y -= 6 * mm
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(110 * mm, y, "Total")
        c.drawRightString(W - 20 * mm, y, inr(sub + tax))
    y -= 16 * mm
    c.setFont("Helvetica", 9.5)
    c.drawString(20 * mm, y, "Kindly remit payment to:")
    for line in [f"{d['bank'][0]}", f"A/c No: {d['bank'][1]}", f"IFSC: {d['bank'][2]}"]:
        y -= 5 * mm
        c.drawString(20 * mm, y, line)
    y -= 16 * mm
    c.drawString(20 * mm, y, "Thanking you,")
    c.drawString(20 * mm, y - 5 * mm, "For " + v["name"])


TEMPLATES = {"kaveri": template_kaveri, "nimbus": template_nimbus, "deccan": template_deccan,
             "prism": template_prism, "plain": template_plain}


def render(spec: dict) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Invoice {spec.get('invoice_number') or ''}".strip())
    lines = spec["lines"]
    spec.setdefault("subtotal", round(sum(l["qty"] * l["rate"] for l in lines), 2))
    TEMPLATES[spec["template"]](c, spec)
    c.showPage()
    c.save()
    return buf.getvalue()


def to_scanned(pdf_bytes: bytes, seed: int = 7) -> bytes:
    """Rasterise a PDF page and degrade it to look like a phone/office scan."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter, ImageEnhance

    rnd = random.Random(seed)
    page = pdfium.PdfDocument(pdf_bytes)[0]
    img = page.render(scale=200 / 72).to_pil().convert("L")
    img = img.rotate(0.9, resample=Image.BICUBIC, expand=False, fillcolor=255)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img = ImageEnhance.Contrast(img).enhance(0.85)
    px = img.load()
    w, h = img.size
    for _ in range(w * h // 180):  # speckle noise
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = rnd.choice([150, 180, 205])
    # faint warm paper tone
    img = Image.merge("RGB", (img, img.point(lambda v: int(v * 0.985)), img.point(lambda v: int(v * 0.95))))
    out = io.BytesIO()
    img.save(out, format="PDF", resolution=200)
    return out.getvalue()


KAVERI_BANK = ("HDFC Bank", "50200012345671", "HDFC0001234")
NIMBUS_BANK = ("ICICI Bank", "001205009876", "ICIC0000012")
DECCAN_BANK = ("Axis Bank", "917020045566778", "UTIB0000456")
PRISM_BANK = ("Kotak Mahindra Bank", "4412987766", "KKBK0000458")
SAHYADRI = {"name": "Sahyadri Facility Management Services", "gstin": "29ABCFS2290Q1ZH",
            "addr": "88, 2nd Main, Jayanagar 4th Block, Bengaluru 560011, Karnataka"}
APEX = {"name": "Apex Traders", "gstin": "07AAKFA9031L1Z8",
        "addr": "Shop 12, Naraina Industrial Area Phase II, New Delhi 110028"}

SCENARIOS = [
    dict(file="01_happy_kaveri_stationery.pdf", group="Happy path", title="Clean invoice, exact PO match",
         expect="APPROVED",
         story="Machine-readable PDF from an approved vendor. Every line matches PO-2026-0381 to the rupee.",
         template="kaveri", invoice_number="KOS/26-27/0214", date_str="24/10/2026", po="PO-2026-0381",
         bank=KAVERI_BANK, tax=16920.00,
         lines=[dict(desc="A4 copier paper 75gsm (ream)", hsn="4802", qty=200, rate=245.00),
                dict(desc="Toner cartridge HP 76A", hsn="8443", qty=12, rate=3150.00),
                dict(desc="Whiteboard markers (box of 10)", hsn="9608", qty=40, rate=180.00)]),
    dict(file="02_happy_nimbus_within_tolerance.pdf", group="Happy path", title="Small usage variance, within tolerance",
         expect="APPROVED",
         story="Different layout (rupee symbol, IGST). Usage-based hosting came in Rs. 640 above the PO line: within the 2% tolerance, so it is approved with a note.",
         template="nimbus", invoice_number="NCS-INV-30577", date_str="31 Oct 2026", due_str="15 Nov 2026",
         po="PO-2026-0395", bank=NIMBUS_BANK, tax=37465.20,
         lines=[dict(desc="Cloud hosting - October 2026 (usage-based)", qty=1, rate=185640.00),
                dict(desc="Managed backup service", qty=1, rate=22500.00)]),
    dict(file="03_split_po_deccan_september.pdf", group="Edge case 1 · Split PO", title="Split PO: 2nd of 3 invoices",
         expect="APPROVED",
         story="Blanket freight PO of Rs. 2,50,000 billed monthly. August (Rs. 1,02,000) is already in the ledger. September's Rs. 96,500 fits in the remaining balance. Run this one before the October invoice.",
         template="deccan", invoice_number="DLF/FRT/2026/0963", date_str="30/09/2026", po="PO-2026-0412",
         bank=DECCAN_BANK, tax=11580.00,
         lines=[dict(desc="Freight charges - September 2026 (bundled)", qty=1, rate=96500.00,
                     note="Consolidated: 41 inbound shipments, Hyderabad/Chennai to Bengaluru DC, per annexure")]),
    dict(file="04_split_po_deccan_october.pdf", group="Edge case 1 · Split PO", title="Split PO: 3rd invoice overruns the PO",
         expect="NEEDS_REVIEW",
         story="On its own this invoice looks fine. But together with August and September it takes the PO Rs. 20,500 over its approved value, so it is held for procurement.",
         template="deccan", invoice_number="DLF/FRT/2026/1041", date_str="31/10/2026", po="PO-2026-0412",
         bank=DECCAN_BANK, tax=8640.00,
         lines=[dict(desc="Freight charges - October 2026 (bundled)", qty=1, rate=72000.00,
                     note="Consolidated: 33 inbound shipments incl. Diwali peak surcharge, per annexure")]),
    dict(file="05_duplicate_kaveri_scanned.pdf", group="Edge case 2 · Disguised duplicate",
         title="Scanned resubmission of a paid invoice", expect="REJECTED",
         story="A scanned copy of invoice KOS/26-27/0187, which was paid on 15 Oct, re-sent with the number reformatted as KOS-2627-187. An exact-match duplicate check would miss it.",
         template="kaveri", invoice_number="KOS-2627-187", date_str="18/09/2026", po="PO-2026-0344",
         bank=KAVERI_BANK, tax=8640.00, scanned=True,
         lines=[dict(desc="A4 printer paper 75gsm (ream)", hsn="4802", qty=150, rate=240.00),
                dict(desc="Spiral binding kit", hsn="8472", qty=20, rate=600.00)]),
    dict(file="06_bank_change_prism.pdf", group="Edge case 3 · Bank detail change",
         title="Perfect match, but new bank account", expect="NEEDS_REVIEW",
         story="Amounts match PO-2026-0420 exactly. But the remittance account differs from the vendor master and the invoice asks AP to 'update records' - the classic invoice-fraud pattern. Rs. 10.8 lakh is held for call-back verification.",
         template="prism", invoice_number="PITH/2026-27/0932", date_str="28-10-2026", po="PO-2026-0420",
         bank=("Yes Bank", "045788901123", "YESB0000457"), bank_notice=True, tax=164700.00,
         lines=[dict(desc="Laptop Dell Latitude 5450", qty=10, rate=82000.00),
                dict(desc="USB-C docking station", qty=10, rate=9500.00)]),
    dict(file="07_no_po_reference_sahyadri.pdf", group="Edge case 4 · Missing PO reference",
         title="No PO number, tax-inclusive pricing", expect="APPROVED",
         story="The vendor didn't quote a PO and priced inclusive of GST. The process backs out the tax and searches the vendor's open POs. Exactly one PO fits on amount and description, so it is matched and the inference is flagged.",
         template="plain", vendor=SAHYADRI, invoice_number="SFMS/1026/118", date_str="31-10-2026",
         bank=("State Bank of India", "39876543210", "SBIN0040321"), inclusive=True,
         lines=[dict(desc="Housekeeping & sanitation services - October 2026", qty=1, rate=165200.00)]),
    dict(file="08_price_variance_prism.pdf", group="Other checks", title="Unit price above PO",
         expect="NEEDS_REVIEW",
         story="Monitors billed at Rs. 15,350 against a PO price of Rs. 14,200 (+8.1%). That is outside tolerance, so the process drafts a query to the vendor.",
         template="prism", invoice_number="PITH/2026-27/0941", date_str="30-10-2026", po="PO-2026-0427",
         bank=PRISM_BANK, tax=41445.00,
         lines=[dict(desc="27-inch monitor Dell P2725H", qty=15, rate=15350.00)]),
    dict(file="09_missing_fields_nimbus.pdf", group="Other checks", title="No invoice number or date",
         expect="NEEDS_REVIEW",
         story="A vendor sent a draft with no invoice number or date. An invoice without these can't be booked or tax-claimed, so the process asks the vendor for a corrected copy.",
         template="nimbus", invoice_number=None, date_str=None, po="PO-2026-0409", bank=NIMBUS_BANK, tax=6300.00,
         lines=[dict(desc="Premium support add-on - October 2026", qty=1, rate=35000.00)]),
    dict(file="10_blocked_vendor_apex.pdf", group="Other checks", title="Vendor blocked in master",
         expect="REJECTED",
         story="Apex Traders was blocked after failing KYC re-verification. Their invoice is rejected regardless of PO match.",
         template="plain", vendor=APEX, invoice_number="AT/2026/552", date_str="27-10-2026", po="PO-2026-0398",
         bank=("Punjab National Bank", "0612002100045123", "PUNB0061200"), tax=10800.00,
         lines=[dict(desc="Corrugated boxes (bundle)", qty=300, rate=200.00)]),
]


def main():
    manifest = []
    for s in SCENARIOS:
        spec = dict(s)
        pdf = render(spec)
        if spec.get("scanned"):
            pdf = to_scanned(pdf)
        (OUT / s["file"]).write_bytes(pdf)
        manifest.append({k: s[k] for k in ("file", "group", "title", "expect", "story")})
        print("wrote", s["file"])
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

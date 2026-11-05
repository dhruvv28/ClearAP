"""Reading and extraction.

Two independent readers look at every invoice:
  1. A rule-based parser (labels + patterns + arithmetic checks). Deterministic,
     instant, works offline.
  2. An LLM (Gemini or Claude, when an API key is set), which reads layouts it has never
     seen and returns structured JSON.

Where both readers agree, confidence is high. Where they disagree on a field
the business depends on, that field is flagged for a human instead of guessed.
If the AI call fails or times out, the rule-based reading is used and the run
says so — the process never stops because a model was slow.
"""
import base64
import io
import json
import re
import time

from . import config, llm
from .gstin import validate as gstin_validate
from .normalize import parse_amount, parse_date

GSTIN_ANY = re.compile(r"\b(\d{2} ?[A-Z]{5} ?\d{4} ?[A-Z] ?[0-9A-Z] ?Z ?[0-9A-Z])\b")  # OCR sometimes inserts a space
AMT = r"(?:₹|Rs\.?|INR)?\s*((?:\d{1,3}(?:,\d{2,3})+|\d+)\.\d{2})"
DATE_VAL = r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}[\s\-][A-Za-z]{3,9}[\s\-,]+\d{4})"
NUM_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:₹|Rs\.?)?(\d[\d,]*(?:\.\d+)?)(?![A-Za-z0-9])")

CRITICAL_FIELDS = ["vendor_name", "invoice_number", "invoice_date", "total"]
SCALAR_FIELDS = ["vendor_name", "vendor_gstin", "buyer_gstin", "invoice_number", "invoice_date", "due_date",
                 "po_reference", "subtotal", "tax_total", "total", "bank_name", "bank_account", "ifsc"]


# ============================================================== read document
def read_document(pdf_bytes: bytes) -> dict:
    """Return text plus how it was obtained. Scanned PDFs go through OCR."""
    import pdfplumber

    t0 = time.time()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = len(pdf.pages)
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if len(text.strip()) >= 80:
        return {"method": "text_layer", "text": text, "pages": pages, "ocr_confidence": None,
                "word_conf": {}, "ms": int((time.time() - t0) * 1000), "image_b64": None}

    # No usable text layer -> it's an image. Render and OCR.
    import pypdfium2 as pdfium
    import pytesseract

    doc = pdfium.PdfDocument(pdf_bytes)
    texts, confs, word_conf, image_b64 = [], [], {}, None
    for i in range(len(doc)):
        from PIL import ImageFilter
        img = doc[i].render(scale=3.0).to_pil().convert("L").filter(ImageFilter.MedianFilter(3))
        if i == 0:
            buf = io.BytesIO()
            small = img.copy()
            small.thumbnail((1600, 1600))
            small.save(buf, format="PNG")
            image_b64 = base64.b64encode(buf.getvalue()).decode()
        texts.append(pytesseract.image_to_string(img, config="--psm 4"))
        data = pytesseract.image_to_data(img, config="--psm 4", output_type=pytesseract.Output.DICT)
        for w, c in zip(data["text"], data["conf"]):
            w = w.strip()
            try:
                c = float(c)
            except (TypeError, ValueError):
                continue
            if w and c >= 0:
                confs.append(c)
                word_conf[w] = max(word_conf.get(w, 0), c)
    mean = round(sum(confs) / len(confs), 1) if confs else 0.0
    return {"method": "ocr", "text": "\n".join(texts), "pages": pages, "ocr_confidence": mean,
            "word_conf": word_conf, "ms": int((time.time() - t0) * 1000), "image_b64": image_b64}


# ============================================================== rule-based parser
def _first(patterns, lines, flags=re.I):
    for pat in patterns:
        rx = re.compile(pat, flags)
        for ln in lines:
            m = rx.search(ln)
            if m:
                return m.group(1).strip(), ln
    return None, None


def _last_amount(line: str):
    found = re.findall(AMT, line)
    return parse_amount(found[-1]) if found else None


def parse_line_items(lines: list[str]) -> list[dict]:
    """A row is a line item if its last three numbers satisfy qty x rate = amount."""
    items = []
    has_hsn = any(re.search(r"\b(HSN|SAC)\b", ln) for ln in lines)
    for ln in lines:
        low = ln.lower()
        if any(k in low for k in ("total", "gst @", "cgst", "sgst", "igst", "taxable", "subtotal")):
            continue
        toks = [(m.group(1), m.start()) for m in NUM_TOKEN.finditer(ln)]
        if len(toks) < 3:
            continue
        (q, qpos), (r, _), (a, _) = toks[-3], toks[-2], toks[-1]
        qty, rate, amt = parse_amount(q), parse_amount(r), parse_amount(a)
        if not qty or not rate or not amt or qty > 100000:
            continue
        if abs(qty * rate - amt) > max(1.0, 0.005 * amt):
            continue
        desc = ln[:qpos].strip()
        desc = re.sub(r"^\W*\d{1,3}\s+(?=[A-Za-z0-9])", "", desc)       # leading S.No
        if has_hsn:
            desc = re.sub(r"\s+\d{4,8}$", "", desc)                      # trailing HSN/SAC column
        desc = re.sub(r"\s+(nos|pcs|units?|lot|reams?)$", "", desc, flags=re.I)
        desc = desc.strip(" -|:‘'\"")
        if len(desc) < 3:
            continue
        items.append({"description": desc, "quantity": qty, "unit_price": rate, "amount": amt})
    return items


def heuristic_parse(text: str, word_conf: dict | None = None) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    f: dict = {}
    src: dict = {}

    v, _ = _first([r"(?:tax\s+)?invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-_.]*\d[A-Z0-9/\-_.]*)",
                   r"\bbill\s*no\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-_.]*\d[A-Z0-9/\-_.]*)"], lines)
    f["invoice_number"], src["invoice_number"] = v, "label" if v else None

    v, _ = _first([rf"(?:invoice\s+date|issue\s+date|dated)\s*[:\-]?\s*{DATE_VAL}",
                   rf"(?<!due\s)\bdate\s*[:\-]?\s*{DATE_VAL}"], lines)
    f["invoice_date"], src["invoice_date"] = parse_date(v), "label" if v else None
    f["invoice_date_raw"] = v
    if v and not f["invoice_date"]:
        src["invoice_date"] = None  # printed but unreadable (e.g. OCR read 18 as 48)
    v, _ = _first([rf"due\s+date\s*[:\-]?\s*{DATE_VAL}"], lines)
    f["due_date"] = parse_date(v)

    po_val = r"(PO[\s\-/]?\d{4}[\s\-/]\d{3,5}|[A-Z]{2,4}[\-/]\d{2,4}[\-/]\d{2,6})"
    v, _ = _first([rf"(?:p\.?\s?o\.?\s*(?:no\.?|number|#)|purchase\s+order(?:\s+no\.?)?|your\s+po|order\s+ref)\s*[:\-]?\s*{po_val}"], lines)
    if v:
        src["po_reference"] = "label"
    else:
        m = re.search(r"\b(PO[\s\-/]?\d{4}[\s\-/]\d{3,5})\b", text, re.I)
        v = m.group(1) if m else None
        src["po_reference"] = "pattern" if v else None
    if v:
        d = re.findall(r"\d+", v)
        v = f"PO-{d[0]}-{d[1].zfill(4)}" if len(d) == 2 and v.upper().startswith("PO") else v.upper()
    f["po_reference"] = v

    gstins = list(dict.fromkeys(g.replace(" ", "") for g in GSTIN_ANY.findall(text.upper())))
    buyer = next((g for g in gstins if g == config.BUYER_GSTIN), None)
    if not buyer:
        # A GSTIN close to ours is probably ours, mis-read
        buyer = next((g for g in gstins if g[:2] == config.BUYER_STATE_CODE and "AAACM" in g), None)
    vendor_g = next((g for g in gstins if g != buyer), None)
    f["vendor_gstin"], f["buyer_gstin"] = vendor_g, buyer
    src["vendor_gstin"] = "pattern" if vendor_g else None

    # Header block -> vendor name: the first line that looks like a company name.
    name = None
    for ln in lines[:6]:
        m = re.search(r"([A-Z][A-Za-z&.,' ]+?(?:Pvt\.? Ltd|Private Limited|LLP|Ltd|Services|Traders|Solutions|Supplies)\b)", ln, re.I)
        if m:
            name = m.group(1).strip()
            break
    if not name and lines:
        name = re.sub(r"\s+(TAX\s+)?INVOICE.*$", "", lines[0], flags=re.I).strip()
    f["vendor_name"], src["vendor_name"] = name, "header" if name else None

    # Amounts
    total = sub = None
    for ln in lines:
        low = ln.lower()
        if sub is None and re.search(r"\b(sub\s*total|subtotal|taxable\s+(value|amount))\b", low):
            sub = _last_amount(ln)
        elif total is None and "sub" not in low and re.search(
                r"(grand\s+total|total\s+amount\s+payable|amount\s+due|invoice\s+total|total\s+amount|^total\b|net\s+payable)", low):
            total = _last_amount(ln)
    tax_lines = []
    for ln in lines:
        for m in re.finditer(rf"\b(CGST|SGST|IGST|UTGST)\b\s*@?\s*(\d+(?:\.\d+)?)\s*%\s*(?:[A-Za-z ]*?)\s*{AMT}", ln, re.I):
            tax_lines.append({"type": m.group(1).upper(), "rate": float(m.group(2)), "amount": parse_amount(m.group(3))})
    inclusive = bool(re.search(r"inclusive\s+of\s+(?:gst|tax)", text, re.I))
    tax_total = round(sum(t["amount"] for t in tax_lines), 2) if tax_lines else None
    src.update(subtotal="label" if sub else None, total="label" if total else None,
               tax_total="label" if tax_total else None)
    if inclusive and total and sub is None:
        if tax_total is None:
            m = re.search(r"inclusive\s+of\s+gst\s*@?\s*(\d+(?:\.\d+)?)\s*%", text, re.I)
            rate = float(m.group(1)) if m else 18.0
            tax_total = round(total - total / (1 + rate / 100), 2)
            src["tax_total"] = "derived"
        sub = round(total - tax_total, 2)
        src["subtotal"] = "derived"
    if total is None and sub is not None and tax_total is not None:
        total, src["total"] = round(sub + tax_total, 2), "derived"
    if sub is None and total is not None and tax_total is not None:
        sub, src["subtotal"] = round(total - tax_total, 2), "derived"
    if tax_total is None and sub is not None and total is not None:
        tax_total, src["tax_total"] = round(total - sub, 2), "derived"
    f.update(subtotal=sub, total=total, tax_total=tax_total, tax_lines=tax_lines, tax_inclusive=inclusive)

    # Bank
    v, _ = _first([r"(?:a[/l1i]c\s*no\.?|account\s+(?:no\.?|number)|bank\s+a/?c)\s*[:\-]?\s*([0-9][0-9 ]{7,20}[0-9])"], lines)
    f["bank_account"] = re.sub(r"\s", "", v) if v else None
    m = re.search(r"\b([A-Z]{4}0[A-Z0-9]{6})\b", text)
    f["ifsc"] = m.group(1) if m else None
    v, _ = _first([r"bank(?:\s+name)?\s*:\s*([A-Za-z][A-Za-z .&]*?bank(?: of india)?)\b"], lines)
    if not v:
        for m in re.finditer(r"((?:[A-Z][A-Za-z]+ ){1,3}Bank(?: of India)?)\b", text):
            cand = re.sub(r"^(Remit(?:tance)?|Details|Pay|to|Bank)\s+", "", m.group(1).strip())
            if cand.lower() != "bank":
                v = cand
                break
    f["bank_name"] = v
    f["bank_change_notice"] = bool(re.search(
        r"(bank(?:ing)?\s+details\s+(?:have\s+)?changed|new\s+bank\s+(?:details|account)|updated\s+bank|change\s+(?:in|of)\s+bank)",
        text, re.I))
    for k in ("bank_account", "ifsc", "bank_name"):
        src[k] = "pattern" if f.get(k) else None

    f["line_items"] = parse_line_items(lines)
    f["currency"] = "INR"

    conf = {}
    for k in SCALAR_FIELDS:
        val = f.get(k)
        if val in (None, ""):
            conf[k] = 0.0
            continue
        base = {"label": 0.95, "header": 0.85, "pattern": 0.85, "derived": 0.8}.get(src.get(k), 0.8)
        if word_conf:  # OCR: scale by how sure tesseract was about the characters in this value
            parts = [p for p in re.split(r"\s+", str(val)) if p]
            cs = [word_conf[p] for p in parts if p in word_conf]
            ocr_c = (sum(cs) / len(cs) / 100.0) if cs else 0.75
            base = round(base * (0.55 + 0.45 * ocr_c), 3)
        conf[k] = round(base, 3)
    if f.get("vendor_gstin") and word_conf:  # an OCR'd GSTIN that fails its checksum is probably misread
        ok, _ = gstin_validate(f["vendor_gstin"])
        if not ok:
            conf["vendor_gstin"] = min(conf["vendor_gstin"], 0.4)
    return {"fields": f, "confidence": conf, "source": src}


# ============================================================== LLM extraction
LLM_PROMPT = """You are an accounts-payable data extraction engine for an Indian company (buyer: {buyer}, GSTIN {buyer_gstin}).
Extract the invoice below into JSON. Rules:
- Only report what is printed on the invoice. If a field is absent, use null. Never invent values.
- Dates as YYYY-MM-DD (Indian invoices are day-first: 05/10/2026 is 5 Oct 2026).
- Amounts as plain numbers in rupees (no commas or symbols).
- vendor_gstin is the SUPPLIER's GSTIN, not the buyer's.
- If prices are stated as inclusive of GST, set tax_inclusive=true and give subtotal = pre-tax value.
- bank_change_notice = true if the invoice says bank/remittance details have changed.
- confidence: your 0-1 confidence for each scalar field.

Return ONLY a JSON object with keys:
vendor_name, vendor_gstin, buyer_gstin, invoice_number, invoice_date, due_date, po_reference,
line_items (list of {{description, quantity, unit_price, amount}}), subtotal, tax_lines (list of {{type, rate, amount}}),
tax_total, total, tax_inclusive, bank_name, bank_account, ifsc, bank_change_notice, confidence (object).

INVOICE TEXT:
<<<
{text}
>>>"""


def llm_available() -> bool:
    return llm.available()


def llm_extract(text: str, image_b64: str | None) -> dict:
    prompt = LLM_PROMPT.format(buyer=config.BUYER_NAME, buyer_gstin=config.BUYER_GSTIN, text=text[:15000])
    out = llm.complete(prompt, image_b64, max_tokens=8000, json_mode=True)  # headroom for models that think before answering
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError("model did not return JSON")
    data = json.loads(m.group(0))
    # normalise types
    for k in ("subtotal", "tax_total", "total"):
        data[k] = parse_amount(data.get(k))
    for k in ("invoice_date", "due_date"):
        data[k] = parse_date(data.get(k)) or data.get(k)
    items = []
    for li in data.get("line_items") or []:
        items.append({"description": str(li.get("description") or "").strip(),
                      "quantity": parse_amount(li.get("quantity")), "unit_price": parse_amount(li.get("unit_price")),
                      "amount": parse_amount(li.get("amount"))})
    data["line_items"] = items
    data["tax_lines"] = [{"type": str(t.get("type") or "").upper(), "rate": parse_amount(t.get("rate")),
                          "amount": parse_amount(t.get("amount"))} for t in data.get("tax_lines") or []]
    if data.get("po_reference"):
        d = re.findall(r"\d+", data["po_reference"])
        if data["po_reference"].upper().startswith("PO") and len(d) == 2:
            data["po_reference"] = f"PO-{d[0]}-{d[1].zfill(4)}"
    if data.get("bank_account"):
        data["bank_account"] = re.sub(r"\D", "", str(data["bank_account"]))
    return data


def _same(a, b) -> bool:
    if a in (None, "") or b in (None, ""):
        return False
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        fa, fb = parse_amount(a), parse_amount(b)
        return fa is not None and fb is not None and abs(fa - fb) <= 0.51
    na, nb = re.sub(r"[^A-Z0-9]", "", str(a).upper()), re.sub(r"[^A-Z0-9]", "", str(b).upper())
    return na == nb or (len(na) > 6 and (na in nb or nb in na))


AMOUNT_FIELDS = ("subtotal", "tax_total", "total")
# Fields that change what happens to the money. A disagreement here goes to a human.
DECISION_FIELDS = {"invoice_number", "invoice_date", "vendor_gstin", "po_reference", "subtotal", "tax_total",
                   "total", "bank_account", "ifsc"}


def _reconciles(amts: dict, items, inclusive: bool) -> bool:
    sub, tax, tot = (parse_amount(amts.get(k)) for k in AMOUNT_FIELDS)
    if None in (sub, tax, tot) or abs(sub + tax - tot) > 1.0:
        return False
    target = tot if inclusive else sub
    return not items or _lines_sum_to(items, target)


def _lines_sum_to(items, target) -> bool:
    if not items or target is None:
        return False
    amts = [parse_amount(li.get("amount")) for li in items]
    return None not in amts and abs(sum(amts) - float(target)) <= 1.0


def extract(doc: dict) -> dict:
    """Run both readers and reconcile. Returns fields, per-field confidence, and notes."""
    rules = heuristic_parse(doc["text"], doc.get("word_conf"))
    result = {"fields": rules["fields"], "confidence": rules["confidence"], "agreement": {},
              "mode": "rules", "model": None, "llm_error": None, "llm_ms": None}
    if not llm_available():
        return result

    t0 = time.time()
    try:
        ai = llm_extract(doc["text"], doc.get("image_b64"))
    except Exception as e:  # network, auth, timeout, bad JSON -> fall back, but say so
        result["llm_error"] = f"{llm.display_name()} {llm.friendly_error(e)}"
        result["mode"] = "rules (AI unavailable)"
        return result
    result["llm_ms"] = int((time.time() - t0) * 1000)
    result["mode"], result["model"] = "ai+rules", f"{llm.display_name()} {llm.model_name()}"

    merged = dict(rules["fields"])
    conf, agreement = {}, {}
    ai_conf = ai.get("confidence") or {}
    for k in SCALAR_FIELDS:
        a, r = ai.get(k), rules["fields"].get(k)
        if a in (None, "") and r in (None, ""):
            conf[k], agreement[k] = 0.0, "both_missing"
        elif _same(a, r):
            merged[k] = a if k != "vendor_name" else (a or r)
            conf[k], agreement[k] = 0.98, "agree"
        elif a not in (None, "") and r in (None, ""):
            merged[k] = a
            conf[k], agreement[k] = round(min(0.9, float(ai_conf.get(k, 0.85) or 0.85)), 3), "ai_only"
        elif a in (None, "") and r not in (None, ""):
            merged[k] = r
            conf[k], agreement[k] = round(rules["confidence"].get(k, 0.7) * 0.85, 3), "rules_only"
        elif k not in DECISION_FIELDS:
            # Cosmetic differences (name casing, bank name wording) don't change the decision.
            merged[k] = r if (k == "buyer_gstin" and r == config.BUYER_GSTIN) else a
            conf[k], agreement[k] = 0.85, "minor_diff"
            agreement[k + "_alt"] = r if merged[k] == a else a
        else:
            merged[k] = a
            conf[k], agreement[k] = 0.55, "disagree"
            agreement[k + "_alt"] = r

    # Money fields: if the readers disagree, use arithmetic as the tie-breaker. Only the
    # reading whose subtotal + tax = total (and whose lines add up) can be right.
    if any(agreement.get(k) == "disagree" for k in AMOUNT_FIELDS):
        ai_set = {k: ai.get(k) for k in AMOUNT_FIELDS}
        ru_set = {k: rules["fields"].get(k) for k in AMOUNT_FIELDS}
        ok_ai = _reconciles(ai_set, ai.get("line_items"), bool(ai.get("tax_inclusive")))
        ok_ru = _reconciles(ru_set, rules["fields"].get("line_items"), rules["fields"].get("tax_inclusive", False))
        if ok_ai != ok_ru:
            winner, label = (ai_set, "AI") if ok_ai else (ru_set, "rule-based")
            for k in AMOUNT_FIELDS:
                if agreement.get(k) == "disagree":
                    loser = ru_set[k] if ok_ai else ai_set[k]
                    merged[k] = winner[k]
                    conf[k], agreement[k] = 0.9, "resolved"
                    agreement[k + "_alt"] = loser
                    agreement[k + "_why"] = f"used the {label} reading because only it reconciles (subtotal + tax = total)"

    # Line items: prefer whichever set actually adds up to the invoice.
    target_ai = (merged.get("total") if merged.get("tax_inclusive") else merged.get("subtotal"))
    r_items, a_items = rules["fields"].get("line_items") or [], ai.get("line_items") or []
    if _lines_sum_to(r_items, target_ai):
        merged["line_items"] = r_items
    elif _lines_sum_to(a_items, target_ai):
        merged["line_items"] = a_items
    elif a_items and all(li.get("amount") is not None for li in a_items):
        merged["line_items"] = a_items
    if ai.get("tax_lines") and all(t.get("amount") is not None for t in ai["tax_lines"]):
        merged["tax_lines"] = ai["tax_lines"]
    merged["tax_inclusive"] = bool(ai.get("tax_inclusive")) or rules["fields"].get("tax_inclusive", False)
    merged["bank_change_notice"] = bool(ai.get("bank_change_notice")) or rules["fields"].get("bank_change_notice", False)
    if doc.get("method") == "ocr" and merged.get("vendor_gstin") and not gstin_validate(merged["vendor_gstin"])[0]:
        conf["vendor_gstin"] = min(conf.get("vendor_gstin", 0.4), 0.4)
    result.update(fields=merged, confidence=conf, agreement=agreement)
    return result

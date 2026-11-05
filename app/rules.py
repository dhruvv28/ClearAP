"""The decision logic. Deliberately deterministic: the AI reads the invoice,
these rules decide what happens to the money. Every rule returns a Check
that says what it looked at, what it found, and who needs to act.

Outcome severity:   pass < info < warn < hold < reject
  hold   -> invoice goes to a named owner with a reason; payment is blocked
  reject -> invoice is returned; it must not be paid in its current form
"""
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from . import config, db
from .gstin import STATE_CODES, validate as gstin_validate
from .normalize import (days_between, digits, fmt_inr, norm_invoice_number, similarity,
                        text_similarity)

SEVERITY = {"pass": 0, "info": 1, "warn": 2, "hold": 3, "reject": 4}

OWNERS = {
    "ap": "AP clerk",
    "procurement": "Procurement",
    "vendor_master": "Vendor master team",
    "treasury": "Treasury / fraud control",
    "vendor": "Vendor (query drafted)",
}


@dataclass
class Check:
    id: str
    stage: str
    name: str
    outcome: str            # pass | info | warn | hold | reject
    message: str
    owner: str | None = None
    vendor_action: str | None = None   # what we need from the vendor, if anything
    data: dict = field(default_factory=dict)

    def dict(self):
        return asdict(self)


# ===================================================================== vendor
def match_vendor(fields: dict) -> dict:
    """Identify the supplier. GSTIN is authoritative; name is the fallback."""
    g = (fields.get("vendor_gstin") or "").upper()
    for v in db.VENDORS:
        if g and v["gstin"] == g:
            return {"vendor": v, "method": "GSTIN", "score": 1.0}
    name = fields.get("vendor_name") or ""
    best, best_s = None, 0.0
    for v in db.VENDORS:
        for cand in [v["name"]] + v["aliases"]:
            s = text_similarity(name, cand)
            if name and cand.lower() in name.lower():
                s = max(s, 0.95)
            if s > best_s:
                best, best_s = v, s
    if best and best_s >= 0.75:
        return {"vendor": best, "method": "name", "score": round(best_s, 2)}
    return {"vendor": None, "method": None, "score": round(best_s, 2),
            "closest": best["name"] if best and best_s >= 0.5 else None}


def _show(k, v):
    return fmt_inr(v) if k in ("subtotal", "tax_total", "total") and isinstance(v, (int, float)) else v


# ===================================================================== document checks
def looks_like_invoice(f: dict, text: str) -> bool:
    signals = [bool(f.get("total")), bool(f.get("invoice_number")), bool(f.get("vendor_gstin")),
               bool(f.get("line_items")), bool(f.get("tax_lines"))]
    return sum(signals) >= 2


def document_checks(f: dict, conf: dict, agreement: dict, text: str = "") -> list[Check]:
    out = []
    S = "validate"
    if not looks_like_invoice(f, text):
        blank = len(text.strip()) < 20
        return [Check("is_invoice", S, "Document is an invoice", "hold",
                      ("The document is blank or unreadable." if blank else
                       "This doesn't look like an invoice: no total, invoice number, GSTIN, line items or tax lines were found.")
                      + " Nothing was matched or posted. Routed to the AP inbox for a person to look at.",
                      OWNERS["ap"])]

    missing = [k for k, label in (("invoice_number", "invoice number"), ("invoice_date", "invoice date"),
                                  ("total", "invoice total")) if not f.get(k)]
    if missing:
        nice = {"invoice_number": "invoice number", "invoice_date": "invoice date", "total": "invoice total"}
        unreadable = [k for k in missing if k == "invoice_date" and f.get("invoice_date_raw")]
        msg = "Missing on the invoice: " + ", ".join(nice[k] for k in missing) + "."
        if unreadable:
            msg += f" (Date printed as '{f['invoice_date_raw']}', which is not a valid date.)"
        msg += " An invoice without these can't be booked, matched for duplicates, or used for a GST input-tax claim."
        out.append(Check("required_fields", S, "Mandatory fields present", "hold", msg, OWNERS["vendor"],
                         vendor_action="Send a corrected tax invoice showing " + " and ".join(nice[k] for k in missing) + ".",
                         data={"missing": missing}))
    else:
        out.append(Check("required_fields", S, "Mandatory fields present", "pass",
                         "Invoice number, date and total are all present."))

    low = [k for k in ("vendor_name", "invoice_number", "invoice_date", "total", "vendor_gstin", "bank_account")
           if f.get(k) and conf.get(k, 1) < config.MIN_FIELD_CONFIDENCE]
    disagree = [k for k, v in agreement.items() if v == "disagree"]
    if low or disagree:
        parts = []
        if low:
            parts.append("low read confidence on " + ", ".join(low))
        if disagree:
            parts.append("the AI and rule-based readers disagree on " + ", ".join(
                f"{k} ('{_show(k, f.get(k))}' vs '{_show(k, agreement.get(k + '_alt'))}')" for k in disagree))
        out.append(Check("extraction_confidence", S, "Extraction confidence", "hold",
                         "Needs a human glance: " + "; ".join(parts) + ".", OWNERS["ap"],
                         data={"low": low, "disagree": disagree}))
    else:
        resolved = [k for k, v in agreement.items() if v == "resolved"]
        if resolved:
            out.append(Check("extraction_confidence", S, "Extraction confidence", "info",
                             "The two readers disagreed on " + ", ".join(
                                 f"{k} ({_show(k, f.get(k))} vs {_show(k, agreement.get(k + '_alt'))})" for k in resolved)
                             + ". Resolved by arithmetic: only one reading makes subtotal + tax = total.",
                             data={"resolved": resolved}))
        else:
            out.append(Check("extraction_confidence", S, "Extraction confidence", "pass",
                             "All key fields were read with high confidence."))

    ok, why = gstin_validate(f.get("vendor_gstin"))
    if ok:
        out.append(Check("gstin_valid", S, "Supplier GSTIN valid", "pass",
                         f"{f['vendor_gstin']} passes format and checksum ({STATE_CODES.get(f['vendor_gstin'][:2], 'state ' + f['vendor_gstin'][:2])})."))
    else:
        out.append(Check("gstin_valid", S, "Supplier GSTIN valid", "hold",
                         f"Supplier GSTIN is {why}" + (f" ('{f.get('vendor_gstin')}')" if f.get("vendor_gstin") else "") +
                         ". Input tax credit can't be claimed on an invalid GSTIN.", OWNERS["ap"],
                         vendor_action="Confirm your GSTIN on a corrected invoice." if why == "missing" else None))

    if f.get("buyer_gstin") == config.BUYER_GSTIN:
        out.append(Check("billed_to_us", S, "Billed to the right entity", "pass",
                         f"Addressed to {config.BUYER_NAME} ({config.BUYER_GSTIN})."))
    else:
        out.append(Check("billed_to_us", S, "Billed to the right entity", "hold",
                         f"Buyer GSTIN on the invoice is '{f.get('buyer_gstin') or 'not shown'}', expected {config.BUYER_GSTIN}.",
                         OWNERS["vendor"], vendor_action=f"Re-issue the invoice to {config.BUYER_NAME}, GSTIN {config.BUYER_GSTIN}."))

    # arithmetic
    issues = []
    items = f.get("line_items") or []
    sub, tax, tot = f.get("subtotal"), f.get("tax_total"), f.get("total")
    if items:
        s_items = round(sum(li.get("amount") or 0 for li in items), 2)
        target = tot if f.get("tax_inclusive") else sub
        if target and abs(s_items - target) > 1.0:
            issues.append(f"line items add up to {fmt_inr(s_items)} but the invoice states {fmt_inr(target)}")
    if sub is not None and tax is not None and tot is not None and abs(sub + tax - tot) > 1.0:
        issues.append(f"subtotal {fmt_inr(sub)} + tax {fmt_inr(tax)} ≠ total {fmt_inr(tot)}")
    if issues:
        out.append(Check("arithmetic", S, "Invoice arithmetic", "hold", "Totals don't add up: " + "; ".join(issues) + ".",
                         OWNERS["vendor"], vendor_action="The totals on the invoice don't reconcile; please send a corrected invoice."))
    elif sub is not None and tot is not None:
        out.append(Check("arithmetic", S, "Invoice arithmetic", "pass",
                         "Line items, tax and total reconcile." + (" Prices are GST-inclusive; tax backed out." if f.get("tax_inclusive") else "")))

    # GST type: intra-state = CGST+SGST, inter-state = IGST
    types = {t.get("type") for t in f.get("tax_lines") or []}
    vg = f.get("vendor_gstin") or ""
    if types and vg[:2].isdigit():
        intra = vg[:2] == config.BUYER_STATE_CODE
        wrong = (intra and "IGST" in types) or (not intra and types & {"CGST", "SGST"})
        exp = "CGST + SGST" if intra else "IGST"
        if wrong:
            out.append(Check("gst_type", S, "Correct GST type", "hold",
                             f"Supplier is in {STATE_CODES.get(vg[:2], vg[:2])} and we are in Karnataka, so {exp} applies, "
                             f"but the invoice charges {' + '.join(sorted(types))}.", OWNERS["vendor"],
                             vendor_action=f"Re-issue with {exp} (place of supply rules)."))
        else:
            out.append(Check("gst_type", S, "Correct GST type", "pass",
                             f"{exp} charged, consistent with {'intra' if intra else 'inter'}-state supply."))

    d = f.get("invoice_date")
    if d:
        dd = date.fromisoformat(d)
        if dd > config.today():
            out.append(Check("date_sane", S, "Invoice date plausible", "hold", f"Invoice is dated in the future ({d}).", OWNERS["ap"]))
        elif (config.today() - dd).days > config.STALE_INVOICE_DAYS:
            out.append(Check("date_sane", S, "Invoice date plausible", "warn",
                             f"Invoice is {(config.today() - dd).days} days old; GST credit window may be at risk."))
    return out


# ===================================================================== vendor checks
def vendor_checks(f: dict, vm: dict) -> list[Check]:
    S = "vendor"
    v = vm["vendor"]
    if not v:
        hint = f" Closest name in master: {vm['closest']}." if vm.get("closest") else ""
        return [Check("vendor_known", S, "Vendor in master", "hold",
                      f"'{f.get('vendor_name')}' ({f.get('vendor_gstin') or 'no GSTIN'}) is not an onboarded vendor.{hint} "
                      "Route to vendor onboarding before any payment.", OWNERS["vendor_master"])]
    out = [Check("vendor_known", S, "Vendor in master", "pass",
                 f"Matched {v['name']} ({v['vendor_id']}) by {vm['method']}.", data={"vendor_id": v["vendor_id"]})]
    if v["status"] != "approved":
        out.append(Check("vendor_status", S, "Vendor status", "reject",
                         f"{v['name']} is {v['status'].upper()} in the vendor master: {v['status_note']}. "
                         "No payments may be released.", OWNERS["vendor_master"]))
    else:
        out.append(Check("vendor_status", S, "Vendor status", "pass", "Vendor is active and approved for payment."))

    g = (f.get("vendor_gstin") or "").upper()
    if g and g != v["gstin"]:
        out.append(Check("gstin_master", S, "GSTIN matches master", "hold",
                         f"Invoice GSTIN {g} differs from master record {v['gstin']}.", OWNERS["vendor_master"]))
    elif g:
        out.append(Check("gstin_master", S, "GSTIN matches master", "pass", "GSTIN matches the vendor master."))

    acct, ifsc = f.get("bank_account"), (f.get("ifsc") or "").upper()
    if not acct:
        out.append(Check("bank_master", S, "Bank details match master", "info",
                         "No bank details on the invoice; payment will go to the account on file."))
    elif acct.lstrip("0") == v["bank_account"].lstrip("0") and (not ifsc or ifsc == v["ifsc"]):
        out.append(Check("bank_master", S, "Bank details match master", "pass",
                         f"Remittance account matches the verified account on file ({v['bank_name']} ••••{v['bank_account'][-4:]})."))
    else:
        notice = " The invoice explicitly asks AP to update the account, which is the standard pattern in invoice-redirection fraud." \
            if f.get("bank_change_notice") else ""
        out.append(Check(
            "bank_master", S, "Bank details match master", "hold",
            f"Invoice asks for payment to {f.get('bank_name') or 'a bank'} ••••{acct[-4:]} ({ifsc or 'no IFSC'}), "
            f"but the verified account on file is {v['bank_name']} ••••{v['bank_account'][-4:]} ({v['ifsc']}).{notice} "
            f"Do not update the master from the invoice. Call the vendor on the number already on file ({v['phone_on_file']}), "
            "not any number on the invoice, and get the change confirmed in writing.",
            OWNERS["treasury"], data={"invoice_account": acct, "master_account": v["bank_account"],
                                      "invoice_ifsc": ifsc, "master_ifsc": v["ifsc"]}))
    return out


# ===================================================================== PO matching
def _desc_score(f, po) -> float:
    inv_text = " ".join(li.get("description", "") for li in f.get("line_items") or [])
    po_text = po["description"] + " " + " ".join(l["item"] for l in po["lines"])
    return text_similarity(inv_text, po_text) if inv_text else 0.0


def infer_po(f: dict, vendor: dict) -> dict:
    """No PO quoted: score the vendor's open POs on amount fit and description."""
    sub = f.get("subtotal") or 0
    cands = []
    for po in db.POS:
        if po["vendor_id"] != vendor["vendor_id"] or po["status"] != "open":
            continue
        billed, _ = db.billed_against_po(po["po_number"])
        remaining = po["subtotal"] - billed
        if remaining <= 0:
            continue
        amt_fit = max(0.0, 1 - abs(sub - remaining) / max(remaining, 1)) if sub <= remaining + config.tolerance_for(po["subtotal"]) else 0.0
        desc = _desc_score(f, po)
        score = round(0.65 * amt_fit + 0.35 * min(1.0, desc / 0.6), 3)
        cands.append({"po_number": po["po_number"], "description": po["description"], "remaining": remaining,
                      "amount_fit": round(amt_fit, 3), "description_match": round(desc, 3), "score": score})
    cands.sort(key=lambda c: -c["score"])
    top = cands[0] if cands else None
    second = cands[1]["score"] if len(cands) > 1 else 0.0
    confident = bool(top and top["score"] >= config.PO_INFERENCE_MIN_SCORE
                     and top["score"] - second >= config.PO_INFERENCE_MIN_MARGIN)
    return {"candidates": cands, "chosen": top["po_number"] if confident else None, "confident": confident}


def po_checks(f: dict, vendor: dict | None) -> tuple[list[Check], dict | None, dict]:
    S = "po"
    out: list[Check] = []
    meta: dict = {"method": None, "inference": None}
    if not vendor:
        out.append(Check("po_found", S, "Purchase order located", "info", "Skipped: vendor not identified."))
        return out, None, meta

    ref = f.get("po_reference")
    po = None
    if ref:
        po = db.PO_BY_NUMBER.get(ref)
        if not po:
            out.append(Check("po_found", S, "Purchase order located", "hold",
                             f"Invoice quotes {ref}, which doesn't exist in the PO register.", OWNERS["procurement"],
                             vendor_action=f"PO {ref} isn't in our records; please confirm the correct PO number."))
            return out, None, meta
        meta["method"] = "quoted on invoice"
        out.append(Check("po_found", S, "Purchase order located", "pass",
                         f"{po['po_number']} quoted on the invoice: {po['description']}."))
    else:
        inf = infer_po(f, vendor)
        meta["inference"] = inf
        if inf["confident"]:
            po = db.PO_BY_NUMBER[inf["chosen"]]
            top = inf["candidates"][0]
            meta["method"] = "inferred"
            out.append(Check("po_found", S, "Purchase order located", "info",
                             f"No PO quoted. Inferred {po['po_number']} ({po['description']}) from {len(inf['candidates'])} open "
                             f"PO(s) for this vendor: amount fit {top['amount_fit']:.0%}, description match {top['description_match']:.0%}, "
                             f"score {top['score']:.2f} vs next best {inf['candidates'][1]['score'] if len(inf['candidates']) > 1 else 0:.2f}. "
                             "Flagged as inferred in the audit trail.", data=inf))
        else:
            names = ", ".join(f"{c['po_number']} ({c['score']:.2f})" for c in inf["candidates"][:3]) or "none open"
            out.append(Check("po_found", S, "Purchase order located", "hold",
                             f"No PO quoted and no single open PO is a confident match (candidates: {names}).",
                             OWNERS["procurement"], vendor_action="Please quote our purchase order number on the invoice.",
                             data=inf))
            return out, None, meta

    if po["vendor_id"] != vendor["vendor_id"]:
        out.append(Check("po_vendor", S, "PO belongs to this vendor", "hold",
                         f"{po['po_number']} was raised on {db.VENDOR_BY_ID[po['vendor_id']]['name']}, not {vendor['name']}.",
                         OWNERS["procurement"]))
        return out, po, meta

    if po["status"] != "open":
        out.append(Check("po_status", S, "PO open", "hold",
                         f"{po['po_number']} is {po['status'].upper()}: it has been fully billed and closed.",
                         OWNERS["procurement"]))
    else:
        out.append(Check("po_status", S, "PO open", "pass", f"{po['po_number']} is open."))

    # ---- line-level 2-way match ----
    items = f.get("line_items") or []
    if po["blanket"]:
        out.append(Check("line_match", S, "Line items vs PO", "pass",
                         "Blanket PO: billed against the overall value; individual lines aren't priced on the PO."))
    elif items:
        problems, matched = [], []
        divisor = 1 + po["tax_rate"] if f.get("tax_inclusive") else 1
        for li in items:
            best, bs = None, 0.0
            for pl in po["lines"]:
                s = text_similarity(li["description"], pl["item"])
                if s > bs:
                    best, bs = pl, s
            if not best or bs < 0.35:
                problems.append(f"'{li['description']}' is not on the PO")
                continue
            unit = (li.get("unit_price") or 0) / divisor
            var = (unit - best["unit_price"]) / best["unit_price"] * 100 if best["unit_price"] else 0
            qty_over = (li.get("quantity") or 0) - best["quantity"]
            row = {"invoice": li["description"], "po_item": best["item"], "inv_unit": round(unit, 2),
                   "po_unit": best["unit_price"], "variance_pct": round(var, 2), "inv_qty": li.get("quantity"),
                   "po_qty": best["quantity"], "match": round(bs, 2)}
            matched.append(row)
            if var > config.TOLERANCE_PCT:
                problems.append(f"{best['item']}: billed {fmt_inr(unit)}/unit vs PO {fmt_inr(best['unit_price'])} (+{var:.1f}%)")
            if qty_over > 0:
                problems.append(f"{best['item']}: {li['quantity']:g} billed vs {best['quantity']:g} ordered")
        if problems:
            out.append(Check("line_match", S, "Line items vs PO", "hold", "; ".join(problems) + ".", OWNERS["vendor"],
                             vendor_action="Please explain or correct: " + "; ".join(problems) + ".",
                             data={"lines": matched}))
        else:
            worst = max((abs(m["variance_pct"]) for m in matched), default=0)
            out.append(Check("line_match", S, "Line items vs PO", "pass",
                             f"All {len(matched)} line(s) match PO items on quantity and price"
                             + (f" (largest price variance {worst:.2f}%, within {config.TOLERANCE_PCT:g}%)." if worst > 0.01 else "."),
                             data={"lines": matched}))

    # ---- balance (handles split invoices) ----
    sub = f.get("subtotal") or 0
    billed, prior = db.billed_against_po(po["po_number"])
    remaining = round(po["subtotal"] - billed, 2)
    tol = config.tolerance_for(po["subtotal"])
    meta.update(billed_before=billed, remaining_before=remaining, tolerance=tol, prior_invoices=prior)
    prior_txt = ""
    if prior:
        prior_txt = f" Already billed: {fmt_inr(billed)} across {len(prior)} earlier invoice(s) " + \
                    "(" + ", ".join(p["invoice_number"] for p in prior) + ")."
    over = round(sub - remaining, 2)
    if po["status"] == "open":
        if over > tol:
            out.append(Check("po_balance", S, "Within PO value", "hold",
                             f"This invoice ({fmt_inr(sub)} pre-tax) takes {po['po_number']} {fmt_inr(over)} over its approved value "
                             f"of {fmt_inr(po['subtotal'])}, beyond the {fmt_inr(tol)} tolerance. "
                             + (f"{fmt_inr(billed)} was already billed across {len(prior)} earlier invoice(s) "
                                f"({', '.join(p['invoice_number'] for p in prior)}), leaving {fmt_inr(remaining)}. " if prior else
                                f"Only {fmt_inr(remaining)} remains on the PO. ")
                             + "Needs a PO amendment or buyer approval for the excess.",
                             OWNERS["procurement"], data={"over_by": over, "remaining": remaining, "billed": billed}))
        elif over > 0.5:
            out.append(Check("po_balance", S, "Within PO value", "info",
                             f"{fmt_inr(over)} above the remaining PO balance ({abs(over) / po['subtotal']:.2%}), inside the "
                             f"{config.TOLERANCE_PCT:g}% tolerance ({fmt_inr(tol)}).{prior_txt}", data={"over_by": over}))
        else:
            left = remaining - sub
            msg = f"{fmt_inr(sub)} fits within the remaining {fmt_inr(remaining)}.{prior_txt}"
            if left > 1:
                msg += f" {fmt_inr(left)} will remain open on the PO."
            out.append(Check("po_balance", S, "Within PO value", "pass", msg))

    # ---- tax rate ----
    if sub and f.get("tax_total") is not None:
        eff = f["tax_total"] / sub * 100
        if abs(eff - po["tax_rate"] * 100) > config.TAX_RATE_TOLERANCE_PP:
            out.append(Check("tax_rate", S, "Tax rate vs PO", "hold",
                             f"Invoice charges {eff:.2f}% GST; PO was raised at {po['tax_rate'] * 100:.0f}%.", OWNERS["ap"]))
        else:
            out.append(Check("tax_rate", S, "Tax rate vs PO", "pass", f"GST at {eff:.1f}% matches the PO rate."))
    return out, po, meta


# ===================================================================== duplicates
def duplicate_checks(f: dict, vendor: dict | None, po_number: str | None) -> list[Check]:
    S = "duplicate"
    if not vendor:
        return [Check("duplicate", S, "Not a duplicate", "info", "Skipped: vendor not identified.")]
    # Invoices a reviewer rejected don't count: the vendor may legitimately resubmit a corrected copy.
    hist = [h for h in db.ledger_for_vendor(vendor["vendor_id"]) if h["status"] != "rejected"]
    num, num_n = f.get("invoice_number"), norm_invoice_number(f.get("invoice_number"))
    tot, d = f.get("total"), f.get("invoice_date")

    for h in hist:
        if num_n and h["invoice_number_norm"] == num_n:
            state = {"paid": "already PAID", "approved": "already approved for payment",
                     "on_hold": "already received and on hold"}.get(h["status"], h["status"])
            return [Check("duplicate", S, "Not a duplicate", "reject",
                          f"Invoice {num} from {vendor['name']} is {state} (logged {h['invoice_date']}, {fmt_inr(h['total'])}). "
                          "Paying it again would be a duplicate payment.", OWNERS["ap"],
                          data={"match": h, "rule": "exact number"})]

    suspects = []
    for h in hist:
        if not tot or not h["total"]:
            continue
        same_amt = abs(h["total"] - tot) <= max(1.0, 0.005 * tot)
        if not same_amt:
            continue
        dsim = similarity(digits(num), digits(h["invoice_number"])) if num else 0
        gap = days_between(d, h["invoice_date"]) if d else None
        same_po = bool(po_number and h["po_number"] == po_number)
        strong = (dsim >= 0.6 and (same_po or gap == 0)) or (same_po and gap == 0)
        if strong:
            why = []
            if dsim >= 0.6:
                why.append(f"invoice number '{num}' is a reformatted '{h['invoice_number']}' (digit similarity {dsim:.0%})")
            if same_po:
                why.append(f"same PO ({po_number})")
            if gap == 0:
                why.append("same invoice date")
            why.append(f"identical total {fmt_inr(tot)}")
            return [Check("duplicate", S, "Not a duplicate", "reject",
                          f"Matches invoice {h['invoice_number']} dated {h['invoice_date']}, status {h['status'].upper().replace('_', ' ')}: "
                          + "; ".join(why) + ". An exact-match check would have missed this; it would have been paid twice.",
                          OWNERS["ap"], data={"match": h, "rule": "fuzzy", "digit_similarity": round(dsim, 2)})]
        if gap is not None and gap <= config.DUPLICATE_WINDOW_DAYS:
            suspects.append((h, gap))
    if suspects:
        h, gap = suspects[0]
        return [Check("duplicate", S, "Not a duplicate", "hold",
                      f"Same vendor and same total as {h['invoice_number']} from {gap} days apart, with a different invoice "
                      "number. Could be a legitimate recurring charge; confirm before paying.", OWNERS["ap"],
                      data={"match": h, "rule": "possible"})]
    if not num:
        return [Check("duplicate", S, "Not a duplicate", "warn",
                      f"No invoice number, so only amount-based matching was possible against {len(hist)} prior invoice(s); none matched.")]
    return [Check("duplicate", S, "Not a duplicate", "pass",
                  f"Checked against {len(hist)} prior invoice(s) from this vendor: exact number, reformatted number, and "
                  "same-amount-same-period. No match.")]


# ===================================================================== decision
PRIORITY = ["is_invoice", "duplicate", "vendor_status", "bank_master", "vendor_known", "gstin_master",
            "required_fields", "extraction_confidence", "po_vendor", "po_status", "line_match", "po_balance",
            "po_found", "arithmetic", "gstin_valid", "billed_to_us", "gst_type", "tax_rate", "date_sane"]


def decide(checks: list[Check], f: dict, vendor: dict | None, po: dict | None) -> dict:
    worst = max((SEVERITY[c.outcome] for c in checks), default=0)
    blocking = [c for c in checks if SEVERITY[c.outcome] == worst and worst >= SEVERITY["hold"]]
    # When several checks fail, lead with the one that matters most to the business.
    blocking.sort(key=lambda c: PRIORITY.index(c.id) if c.id in PRIORITY else len(PRIORITY))
    if worst >= SEVERITY["reject"]:
        status = "REJECTED"
    elif worst >= SEVERITY["hold"]:
        status = "NEEDS_REVIEW"
    else:
        status = "APPROVED"
    holds = [c for c in checks if c.outcome in ("hold", "reject")]
    owner = blocking[0].owner if blocking else None
    pay_on = None
    if status == "APPROVED" and vendor:
        base = date.fromisoformat(f["invoice_date"]) if f.get("invoice_date") else config.today()
        pay_on = max(base + timedelta(days=vendor["payment_terms_days"]), config.today()).isoformat()
    headline = {
        "APPROVED": "Approved for payment",
        "NEEDS_REVIEW": blocking[0].name + " failed" if blocking else "Needs review",
        "REJECTED": blocking[0].name + " failed" if blocking else "Rejected",
    }[status]
    reason_map = {
        "duplicate": "Duplicate invoice", "vendor_status": "Vendor blocked", "bank_master": "Bank details changed",
        "po_balance": "Exceeds PO balance", "line_match": "Price/qty variance vs PO", "required_fields": "Missing mandatory fields",
        "po_found": "No matching PO", "extraction_confidence": "Low extraction confidence", "vendor_known": "Unknown vendor",
        "po_status": "PO closed", "arithmetic": "Arithmetic mismatch", "gstin_valid": "Invalid GSTIN",
        "gst_type": "Wrong GST type", "tax_rate": "Tax rate mismatch", "billed_to_us": "Wrong bill-to entity",
        "gstin_master": "GSTIN mismatch", "po_vendor": "PO / vendor mismatch", "date_sane": "Invoice date issue",
        "is_invoice": "Not an invoice",
    }
    if blocking:
        headline = reason_map.get(blocking[0].id, headline)
    return {"status": status, "headline": headline, "owner": owner,
            "blocking": [c.id for c in blocking],
            "issues": sorted([c.dict() for c in holds], key=lambda i: PRIORITY.index(i["id"]) if i["id"] in PRIORITY else 99),
            "pay_on": pay_on, "vendor_actions": [c.vendor_action for c in holds if c.vendor_action]}

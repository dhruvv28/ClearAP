"""Orchestrates one invoice through the process, emitting an event per stage
so the UI can show each step as it executes."""
import hashlib
import threading
import time
import uuid
from datetime import datetime

from . import config, db, explain, extract, rules
from .normalize import fmt_inr, norm_invoice_number

STAGES = [
    ("intake", "Receive invoice"),
    ("read", "Read document"),
    ("extract", "Extract fields"),
    ("validate", "Validate invoice"),
    ("vendor", "Verify vendor"),
    ("po", "Match purchase order"),
    ("duplicate", "Duplicate check"),
    ("decide", "Decide"),
    ("route", "Explain & route"),
]
TITLES = dict(STAGES)

STATUS_WORDS = {"APPROVED": "Approved", "NEEDS_REVIEW": "Held for review", "REJECTED": "Rejected", "ERROR": "Error"}

RUNS: dict[str, dict] = {}          # live event buffers: run_id -> {"events": [...], "done": bool}
_pipeline_lock = threading.Lock()   # one invoice at a time so PO balances stay consistent


def _worst(checks):
    return max((c.outcome for c in checks), key=lambda o: rules.SEVERITY[o], default="pass")


class Run:
    def __init__(self, run_id, filename, pdf_bytes, source, fast=False):
        self.id, self.filename, self.pdf, self.source = run_id, filename, pdf_bytes, source
        # Batch runs still show each stage, just with a shorter pause between them.
        self.delay_ms = min(config.STAGE_DELAY_MS, 120) if fast else config.STAGE_DELAY_MS
        self.buf = RUNS[run_id]
        self.t0 = time.time()

    def emit(self, stage, status, summary="", outcome=None, data=None, checks=None, ms=None):
        self.buf["events"].append({
            "type": "stage", "stage": stage, "title": TITLES[stage], "status": status, "summary": summary,
            "outcome": outcome, "data": data or {}, "checks": [c.dict() for c in checks or []], "ms": ms,
            "t": round(time.time() - self.t0, 2)})

    def step(self, stage):
        self.emit(stage, "running")
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000)
        return time.time()

    def execute(self):
        rec = {"id": self.id, "filename": self.filename, "source": self.source,
               "created_at": config.now_iso(), "stages": {}, "policy": config.get_policy()}
        try:
            self._execute(rec)
        except Exception as e:  # never leave the UI spinning
            import traceback
            traceback.print_exc()
            rec["decision"] = {"status": "ERROR", "headline": "Process error", "owner": "AP clerk",
                               "issues": [], "error": f"{type(e).__name__}: {e}"}
            rec["summary"] = f"The process hit an unexpected error ({type(e).__name__}: {e}). The invoice was not posted; route to a human."
            self.buf["events"].append({"type": "error", "message": rec["summary"]})
        rec["duration_ms"] = int((time.time() - self.t0) * 1000)
        rec["events"] = [e for e in self.buf["events"] if e.get("type") == "stage" and e.get("status") == "done"]
        db.save_run(rec)
        self.buf["events"].append({"type": "done", "run_id": self.id, "record": rec})
        self.buf["done"] = True

    def _execute(self, rec):
        # 1 intake ---------------------------------------------------------
        t = self.step("intake")
        sha = hashlib.sha256(self.pdf).hexdigest()
        (config.UPLOAD_DIR / f"{self.id}.pdf").write_bytes(self.pdf)
        intake = {"filename": self.filename, "bytes": len(self.pdf), "sha256": sha[:16], "source": self.source}
        self.emit("intake", "done", f"{self.filename} · {len(self.pdf) / 1024:.0f} KB", "pass", intake,
                  ms=int((time.time() - t) * 1000))
        rec["stages"]["intake"] = intake

        # 2 read -----------------------------------------------------------
        t = self.step("read")
        doc = extract.read_document(self.pdf)
        if doc["method"] == "ocr":
            summ = f"No text layer: scanned image. OCR'd {doc['pages']} page(s), mean word confidence {doc['ocr_confidence']:.0f}%."
            out = "warn" if doc["ocr_confidence"] < 80 else "info"
        else:
            summ = f"Machine-readable PDF. {len(doc['text'])} characters from {doc['pages']} page(s)."
            out = "pass"
        read = {"method": doc["method"], "pages": doc["pages"], "ocr_confidence": doc["ocr_confidence"],
                "chars": len(doc["text"]), "text": doc["text"][:4000]}
        self.emit("read", "done", summ, out, read, ms=int((time.time() - t) * 1000))
        rec["stages"]["read"] = read

        # 3 extract --------------------------------------------------------
        t = self.step("extract")
        ex = extract.extract(doc)
        f, conf = ex["fields"], ex["confidence"]
        mode_txt = {"rules": "Rule-based parser (no AI key configured)",
                    "ai+rules": f"{ex['model']} cross-checked against the rule-based parser",
                    "rules (AI unavailable)": f"{ex.get('llm_error') or 'AI unavailable'}, so the rule-based parser read it alone"}[ex["mode"]]
        n_fields = sum(1 for k in extract.SCALAR_FIELDS if f.get(k) not in (None, ""))
        low = [k for k in extract.CRITICAL_FIELDS if f.get(k) and conf.get(k, 0) < config.MIN_FIELD_CONFIDENCE]
        summ = f"{mode_txt}. {n_fields} fields and {len(f.get('line_items') or [])} line item(s) extracted."
        exd = {"mode": ex["mode"], "model": ex["model"], "llm_ms": ex["llm_ms"], "llm_error": ex["llm_error"],
               "fields": {k: f.get(k) for k in extract.SCALAR_FIELDS}, "confidence": conf,
               "agreement": ex.get("agreement", {}), "line_items": f.get("line_items") or [],
               "tax_lines": f.get("tax_lines") or [], "tax_inclusive": f.get("tax_inclusive"),
               "bank_change_notice": f.get("bank_change_notice"), "invoice_date_raw": f.get("invoice_date_raw")}
        self.emit("extract", "done", summ, "warn" if low or ex.get("llm_error") else "pass", exd,
                  ms=int((time.time() - t) * 1000))
        rec["stages"]["extract"] = exd
        rec["extraction_mode"] = ex["mode"]
        rec["invoice"] = {k: f.get(k) for k in extract.SCALAR_FIELDS}
        rec["invoice"]["line_items"] = f.get("line_items") or []

        all_checks: list[rules.Check] = []

        # 4 validate -------------------------------------------------------
        t = self.step("validate")
        cs = rules.document_checks(f, conf, ex.get("agreement", {}), doc["text"])
        not_invoice = any(c.id == "is_invoice" for c in cs)
        all_checks += cs
        self.emit("validate", "done", self._checks_summary(cs), _worst(cs), checks=cs, ms=int((time.time() - t) * 1000))

        vendor, po, meta = None, None, {}
        rec["vendor"], rec["po"], rec["po_meta"] = None, None, {}
        if not_invoice:
            rec["invoice"]["vendor_name"] = None  # whatever the parser guessed from a non-invoice is meaningless
            for st in ("vendor", "po", "duplicate"):
                self.emit(st, "done", "Skipped: the document isn't an invoice.", "info")
        else:
            # 5 vendor ---------------------------------------------------------
            t = self.step("vendor")
            vm = rules.match_vendor(f)
            vendor = vm["vendor"]
            cs = rules.vendor_checks(f, vm)
            all_checks += cs
            vdata = {"match_method": vm["method"], "match_score": vm["score"],
                     "vendor": {k: vendor[k] for k in ("vendor_id", "name", "gstin", "status", "bank_name", "payment_terms_days")}
                     if vendor else None}
            self.emit("vendor", "done", (f"{vendor['name']} · " if vendor else "Unknown vendor · ") + self._checks_summary(cs),
                      _worst(cs), vdata, cs, ms=int((time.time() - t) * 1000))
            rec["vendor"] = vdata["vendor"]

            # 6 PO ---------------------------------------------------------------
            t = self.step("po")
            cs, po, meta = rules.po_checks(f, vendor)
            all_checks += cs
            pdata = {"method": meta.get("method"), "inference": meta.get("inference"),
                     "billed_before": meta.get("billed_before"), "remaining_before": meta.get("remaining_before"),
                     "tolerance": meta.get("tolerance"),
                     "prior_invoices": [{k: p[k] for k in ("invoice_number", "invoice_date", "subtotal", "status")}
                                        for p in meta.get("prior_invoices") or []],
                     "po": {k: po[k] for k in ("po_number", "description", "subtotal", "tax_rate", "total", "status", "blanket", "lines")}
                     if po else None}
            self.emit("po", "done", (f"{po['po_number']} ({meta['method']}) · " if po else "") + self._checks_summary(cs),
                      _worst(cs), pdata, cs, ms=int((time.time() - t) * 1000))
            rec["po"] = pdata["po"]
            rec["po_meta"] = {k: v for k, v in pdata.items() if k != "po"}

            # 7 duplicate ------------------------------------------------------
            t = self.step("duplicate")
            cs = rules.duplicate_checks(f, vendor, po["po_number"] if po else None)
            all_checks += cs
            self.emit("duplicate", "done", cs[0].message if cs else "", _worst(cs), checks=cs, ms=int((time.time() - t) * 1000))

        # 8 decide ---------------------------------------------------------
        t = self.step("decide")
        decision = rules.decide(all_checks, f, vendor, po)
        decision["notes"] = [c.message for c in all_checks if c.outcome == "info" and c.stage == "po"]
        counts = {o: sum(1 for c in all_checks if c.outcome == o) for o in ("pass", "info", "warn", "hold", "reject")}
        decision["counts"] = counts
        self.emit("decide", "done", f"{decision['status'].replace('_', ' ')}: {decision['headline']}",
                  {"APPROVED": "pass", "NEEDS_REVIEW": "hold", "REJECTED": "reject"}[decision["status"]],
                  {"decision": decision}, ms=int((time.time() - t) * 1000))
        rec["decision"] = decision
        rec["checks"] = [c.dict() for c in all_checks]

        # 9 explain & route ------------------------------------------------
        t = self.step("route")
        summary = explain.template_summary(f, vendor, po, decision, meta)
        polished = explain.llm_polish(summary, {"decision": decision["status"], "issues": [i["message"] for i in decision["issues"]],
                                                "invoice": rec["invoice"], "vendor": vendor and vendor["name"]})
        steps = explain.next_steps(decision, vendor)
        email = explain.vendor_email(f, vendor, decision)
        ledger_action = None
        if vendor and decision["status"] in ("APPROVED", "NEEDS_REVIEW"):
            status = "approved" if decision["status"] == "APPROVED" else "on_hold"
            db.add_ledger({"vendor_id": vendor["vendor_id"], "invoice_number": f.get("invoice_number"),
                           "invoice_number_norm": norm_invoice_number(f.get("invoice_number")),
                           "invoice_date": f.get("invoice_date"), "po_number": po["po_number"] if po else None,
                           "subtotal": f.get("subtotal"), "tax": f.get("tax_total"), "total": f.get("total"),
                           "status": status, "run_id": self.id})
            ledger_action = f"Logged in ledger as {status.upper().replace('_', ' ')}" + (
                f"; {po['po_number']} balance consumed by {fmt_inr(f.get('subtotal'))}" if status == "approved" and po else "")
        else:
            ledger_action = "Not posted to the ledger"
        rec["summary"] = polished or summary
        rec["summary_source"] = "ai" if polished else "template"
        rec["next_steps"] = steps
        rec["vendor_email"] = email
        rec["ledger_action"] = ledger_action
        now = config.now_iso()
        rec["audit"] = [
            {"at": rec["created_at"], "actor": "ClearAP", "action": "Received",
             "detail": f"{self.filename} ({self.source})"},
            {"at": rec["created_at"], "actor": "ClearAP", "action": "Policy applied",
             "detail": config.policy_text(rec["policy"])},
            {"at": now, "actor": "ClearAP", "action": STATUS_WORDS[decision["status"]],
             "detail": decision["headline"] + (f"; assigned to {decision['owner']}" if decision.get("owner") and
                                               decision["status"] != "APPROVED" else "")},
            {"at": now, "actor": "ClearAP", "action": "Ledger", "detail": ledger_action},
        ]
        rec["review_allowed"] = review_options(decision, vendor)
        self.emit("route", "done", ledger_action, "pass",
                  {"summary": rec["summary"], "summary_source": rec["summary_source"], "next_steps": steps,
                   "vendor_email": email, "owner": decision["owner"]}, ms=int((time.time() - t) * 1000))

    @staticmethod
    def _checks_summary(cs):
        n = len(cs)
        bad = [c for c in cs if c.outcome in ("hold", "reject")]
        warn = [c for c in cs if c.outcome == "warn"]
        if bad:
            return f"{len(bad)} of {n} check(s) failed: " + ", ".join(c.name for c in bad)
        if warn:
            return f"{n - len(warn)} of {n} passed, {len(warn)} warning(s)"
        return f"All {n} checks passed"


def start_run(filename: str, pdf_bytes: bytes, source: str, fast: bool = False) -> str:
    run_id = datetime.now().strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    RUNS[run_id] = {"events": [], "done": False}

    def work():
        # One invoice at a time keeps PO balances and the ledger consistent. If another invoice is
        # still running, say so instead of looking frozen.
        if not _pipeline_lock.acquire(blocking=False):
            ahead = sum(1 for k, b in RUNS.items() if k != run_id and not b["done"])
            RUNS[run_id]["events"].append({"type": "queued", "ahead": max(ahead, 1)})
            _pipeline_lock.acquire()
        try:
            Run(run_id, filename, pdf_bytes, source, fast).execute()
        finally:
            _pipeline_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return run_id


def run_sync(filename: str, pdf_bytes: bytes, source: str = "test") -> dict:
    run_id = start_run(filename, pdf_bytes, source)
    while not RUNS[run_id]["done"]:
        time.sleep(0.05)
    return RUNS[run_id]["events"][-1]["record"]


# ---------------------------------------------------------------- human review
NO_RELEASE = {
    "is_invoice": "It isn't an invoice, so there is nothing to pay.",
    "vendor_known": "The vendor isn't onboarded. Onboard them first, then re-run the invoice.",
    "required_fields": "An invoice without a number or date can't be booked. Ask the vendor for a corrected copy.",
}


def review_options(decision: dict, vendor: dict | None) -> dict:
    """What a reviewer may do with a held invoice. Some holds can't be overridden by one person."""
    if decision.get("status") != "NEEDS_REVIEW":
        return {"release": False, "reject": False, "why_not": None}
    ids = [i["id"] for i in decision.get("issues", [])]
    blocker = next((i for i in ids if i in NO_RELEASE), None)
    if blocker or not vendor:
        return {"release": False, "reject": True, "why_not": NO_RELEASE.get(blocker, "No vendor was identified.")}
    hint = None
    if "bank_master" in ids:
        hint = "Record who you called, on which number, and what they confirmed. Payment goes to the account on file until the vendor master is updated."
    elif "po_balance" in ids or "line_match" in ids:
        hint = "Record who approved the excess (e.g. the PO owner) or the vendor's explanation."
    elif "extraction_confidence" in ids:
        hint = "Record which fields you checked against the PDF."
    return {"release": True, "reject": True, "why_not": None, "hint": hint}


def apply_review(run_id: str, action: str, note: str, reviewer: str) -> dict:
    with _pipeline_lock:
        rec = db.get_run(run_id)
        if not rec:
            raise LookupError("Unknown run")
        if rec.get("review"):
            raise ValueError("This invoice has already been reviewed.")
        opts = rec.get("review_allowed") or review_options(rec.get("decision") or {}, rec.get("vendor"))
        if action not in ("release", "reject") or not opts.get(action):
            raise ValueError(opts.get("why_not") or "That action isn't available for this invoice.")
        note = (note or "").strip()
        if len(note) < 10:
            raise ValueError("Add a note of at least a few words; it goes into the audit trail.")
        reviewer = (reviewer or "").strip()[:60] or "AP reviewer"
        inv, po = rec.get("invoice") or {}, rec.get("po")
        if action == "release":
            db.update_ledger_status(run_id, "approved")
            vendor = rec.get("vendor") or {}
            base = inv.get("invoice_date") or config.today().isoformat()
            from datetime import date, timedelta
            pay_on = max(date.fromisoformat(base) + timedelta(days=vendor.get("payment_terms_days", 30)), config.today())
            ledger = "Ledger: ON HOLD → APPROVED" + (f"; {po['po_number']} balance consumed by {fmt_inr(inv.get('subtotal'))}" if po else "")
            rec["review"] = {"action": "release", "final": "RELEASED", "pay_on": pay_on.isoformat()}
        else:
            if db.update_ledger_status(run_id, "rejected"):
                ledger = "Ledger: ON HOLD → REJECTED (a corrected resubmission won't be flagged as a duplicate)"
            else:
                ledger = "Not in the ledger"
            rec["review"] = {"action": "reject", "final": "REJECTED_BY_REVIEWER"}
        now = config.now_iso()
        rec["review"].update(reviewer=reviewer, note=note, at=now, ledger=ledger)
        rec.setdefault("audit", []).extend([
            {"at": now, "actor": reviewer, "action": "Released for payment" if action == "release" else "Rejected",
             "detail": note},
            {"at": now, "actor": "ClearAP", "action": "Ledger", "detail": ledger},
        ])
        db.save_review(rec, rec["review"]["final"])
        return rec

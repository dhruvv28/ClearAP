"""Turn the rule trace into something an AP clerk or a vendor can act on.

The plain-English summary is built from the rule results (so it can't state
anything the rules didn't find). If an API key is set, the LLM rewrites it
for readability under a strict 'only these facts' instruction; any failure
falls back to the template.
"""
import json

from . import config, llm
from .normalize import fmt_inr


def nice_date(iso: str | None) -> str:
    if not iso:
        return "—"
    from datetime import date
    return date.fromisoformat(iso).strftime("%d %b %Y").lstrip("0")


def template_summary(inv: dict, vendor: dict | None, po: dict | None, decision: dict, po_meta: dict) -> str:
    vname = vendor["name"] if vendor else (inv.get("vendor_name") or "an unidentified vendor")
    num = inv.get("invoice_number") or "(no number)"
    amt = fmt_inr(inv.get("total"))
    s = decision["status"]
    if decision.get("blocking") and decision["blocking"][0] == "is_invoice":
        return decision["issues"][0]["message"]
    of = f" for {amt}" if inv.get("total") else ""
    if s == "APPROVED":
        po_txt = ""
        if po:
            po_txt = f" against {po['po_number']}" + (" (PO inferred, not quoted)" if po_meta.get("method") == "inferred" else "")
        notes = [i for i in decision.get("notes", [])]
        extra = (" " + " ".join(notes)) if notes else ""
        return (f"Invoice {num} from {vname}{of} passed every check{po_txt} and is approved. "
                f"It is scheduled for payment on {nice_date(decision['pay_on'])}.{extra}")
    first = decision["issues"][0]["message"] if decision["issues"] else ""
    sentences = [x for x in first.replace("e.g. ", "e.g.\u00a0").split(". ") if x]
    if len(sentences) > 2:  # the full reasoning is in the checks; keep the summary to the point
        first = ". ".join(sentences[:2]).rstrip(".") + "."
    others = len(decision["issues"]) - 1
    verb = "rejected" if s == "REJECTED" else f"held for {decision['owner']}"
    more = f" {others} other issue{'s' if others > 1 else ''} also flagged." if others > 0 else ""
    return f"Invoice {num} from {vname}{of} has been {verb}. {first}{more}"


def next_steps(decision: dict, vendor: dict | None) -> list[str]:
    s = decision["status"]
    if s == "APPROVED":
        return [f"Post to ledger and release in the payment run on {nice_date(decision['pay_on'])}.",
                "No human action needed."]
    steps = []
    ids = {i["id"] for i in decision["issues"]}
    if "is_invoice" in ids:
        return ["AP clerk to open the file and either forward it to the right team or ask the sender for the invoice."]
    if "duplicate" in ids:
        steps.append("Do not pay. Return to vendor as a duplicate, referencing the original invoice.")
    if "vendor_status" in ids:
        steps.append("Do not pay. Vendor master team to inform the requester that this vendor is blocked.")
    if "bank_master" in ids and vendor:
        steps.append(f"Treasury to call {vendor['name']} on {vendor['phone_on_file']} (number on file) to verify the bank change.")
        steps.append("Only after written confirmation: vendor master team updates the account (maker-checker), then release.")
    if "vendor_known" in ids:
        steps.append("Vendor master team to send the onboarding form and run KYC. Once the vendor is approved, re-run this invoice.")
    if "po_balance" in ids:
        steps.append("Procurement to amend the PO or get buyer sign-off for the excess, then re-run.")
    if "po_found" in ids:
        steps.append("Procurement to confirm which PO this invoice belongs to.")
    if "extraction_confidence" in ids:
        steps.append("AP clerk to verify the flagged fields against the PDF (about 30 seconds) and re-run.")
    if decision.get("vendor_actions"):
        steps.append("Send the drafted query to the vendor (below) and park until a response arrives.")
    if not steps:
        steps.append(f"{decision['owner'] or 'AP'} to review the flagged checks and decide.")
    return steps


def vendor_email(inv: dict, vendor: dict | None, decision: dict) -> dict | None:
    acts = decision.get("vendor_actions") or []
    if not acts or decision["status"] == "REJECTED" or not vendor:
        return None
    num = inv.get("invoice_number") or "dated " + (inv.get("invoice_date") or "recently")
    ref = f" (our PO {inv['po_reference']})" if inv.get("po_reference") else ""
    bullets = "\n".join(f"  • {a}" for a in acts)
    body = (f"Dear {vendor['name']} team,\n\n"
            f"Thank you for your invoice {num} for {fmt_inr(inv.get('total'))}{ref}. "
            f"Before we can process it for payment, we need the following:\n\n{bullets}\n\n"
            "Once we receive this, the invoice will be processed in the next payment cycle.\n\n"
            f"Regards,\nAccounts Payable\n{config.BUYER_NAME}")
    return {"to": vendor["name"], "subject": f"Invoice {num}: information needed before payment", "body": body}


def llm_polish(summary: str, facts: dict) -> str | None:
    if not llm.available():
        return None
    prompt = ("Rewrite this accounts-payable decision summary for a busy AP manager in at most 3 short sentences. "
              "Use ONLY the facts given; do not add numbers, causes or recommendations that are not in the facts. "
              "Keep rupee amounts exactly as written.\n\nSUMMARY:\n" + summary +
              "\n\nFACTS (JSON):\n" + json.dumps(facts, default=str)[:6000] + "\n\nReturn only the rewritten summary.")
    try:
        txt = llm.complete(prompt, max_tokens=1500, timeout=12, retries=0).strip()  # cosmetic: never worth a wait
        return txt or None
    except Exception:
        return None

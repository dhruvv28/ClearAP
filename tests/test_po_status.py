"""A PO that's fully invoiced shows as 'fully billed' and stops accepting invoices.

    python -m tests.test_po_status
"""
import importlib.util
import os
import tempfile

os.environ["STAGE_DELAY_MS"] = "0"
if os.environ.get("CLEARAP_TEST_LIVE_AI") != "1":
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.setdefault("CLEARAP_TODAY", "2026-11-02")
os.environ["CLEARAP_DATA_DIR"] = tempfile.mkdtemp(prefix="clearap-postatus-")

from app import config, db, pipeline  # noqa: E402

PO = "PO-2026-0381"


def small_invoice_on_0381() -> bytes:
    """A ₹1,000 top-up against the stationery PO: inside the 2% tolerance, but the PO is used up."""
    spec = importlib.util.spec_from_file_location("gen", config.ROOT / "scripts" / "generate_invoices.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    return gen.render(dict(template="kaveri", invoice_number="KOS/26-27/0233", date_str="30/10/2026", po=PO,
                           bank=gen.KAVERI_BANK, tax=180.00,
                           lines=[dict(desc="Whiteboard markers (box of 10)", hsn="9608", qty=5, rate=200.00)]))


def main():
    db.reset()
    checks = {"PO starts open": db.po_status(db.PO_BY_NUMBER[PO]) == "open"}
    first = pipeline.run_sync("01_happy_kaveri_stationery.pdf",
                              (config.SAMPLES_DIR / "01_happy_kaveri_stationery.pdf").read_bytes())
    checks["invoice 01 still approved"] = first["decision"]["status"] == "APPROVED"
    checks["PO now shows fully billed"] = db.po_status(db.PO_BY_NUMBER[PO]) == "fully billed"
    extra = pipeline.run_sync("topup.pdf", small_invoice_on_0381())
    checks["₹1,000 top-up is held, not approved"] = extra["decision"]["status"] == "NEEDS_REVIEW"
    checks["held for the right reason"] = extra["decision"]["headline"] == "PO closed or fully billed"
    for name, ok in checks.items():
        print(("PASS  " if ok else "FAIL  ") + name)
    print("\nPO STATUS OK" if all(checks.values()) else "\nPO STATUS FAILURES")
    if extra["decision"]["status"] != "NEEDS_REVIEW":
        print(extra["decision"])
    return all(checks.values())


def test_po_status():
    assert main()


if __name__ == "__main__":
    main()

"""Human-review flow: release/reject guardrails, ledger and PO balance effects, audit trail.

    python -m tests.test_review
"""
import os
import tempfile

os.environ["STAGE_DELAY_MS"] = "0"
if os.environ.get("CLEARAP_TEST_LIVE_AI") != "1":  # offline by default: no API calls, no quota used
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.setdefault("CLEARAP_TODAY", "2026-11-02")
os.environ["CLEARAP_DATA_DIR"] = tempfile.mkdtemp(prefix="clearap-review-")

from app import config, db, pipeline  # noqa: E402


def run(f):
    return pipeline.run_sync(f, (config.SAMPLES_DIR / f).read_bytes())


def expect_error(fn, *args):
    try:
        fn(*args)
    except ValueError:
        return True
    return False


def main():
    db.reset()
    run("03_split_po_deccan_september.pdf")
    held = run("04_split_po_deccan_october.pdf")
    missing = run("09_missing_fields_nimbus.pdf")
    dup = run("05_duplicate_kaveri_scanned.pdf")
    checks = {
        "note is required": expect_error(pipeline.apply_review, held["id"], "release", "ok", "t"),
        "can't release an invoice with no number": expect_error(pipeline.apply_review, missing["id"], "release", "looks fine to me", "t"),
        "can't review an auto-rejected duplicate": expect_error(pipeline.apply_review, dup["id"], "reject", "confirmed duplicate", "t"),
    }
    before = db.billed_against_po("PO-2026-0412")[0]
    rec = pipeline.apply_review(held["id"], "release", "PO owner approved the excess by email", "Tester")
    checks["release consumes PO balance"] = db.billed_against_po("PO-2026-0412")[0] == before + 72000
    checks["release recorded in audit trail"] = any(a["action"] == "Released for payment" for a in rec["audit"])
    checks["can't review twice"] = expect_error(pipeline.apply_review, held["id"], "reject", "changed my mind", "t")

    variance = run("08_price_variance_prism.pdf")
    pipeline.apply_review(variance["id"], "reject", "Vendor to re-issue at the PO price", "Tester")
    again = run("08_price_variance_prism.pdf")
    checks["corrected resubmission isn't a duplicate"] = again["decision"]["headline"] != "Duplicate invoice"

    for name, ok in checks.items():
        print(("PASS  " if ok else "FAIL  ") + name)
    print("\nREVIEW FLOW OK" if all(checks.values()) else "\nREVIEW FLOW FAILURES")
    return all(checks.values())


def test_review_flow():
    assert main()


if __name__ == "__main__":
    main()

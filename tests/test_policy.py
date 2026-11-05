"""Editable policy: validation, and that the rules read the live values.

    python -m tests.test_policy
"""
import os
import tempfile

os.environ["STAGE_DELAY_MS"] = "0"
if os.environ.get("CLEARAP_TEST_LIVE_AI") != "1":  # offline by default: no API calls, no quota used
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.setdefault("CLEARAP_TODAY", "2026-11-02")
os.environ["CLEARAP_DATA_DIR"] = tempfile.mkdtemp(prefix="clearap-policy-")

from app import config, db, pipeline  # noqa: E402

SAMPLE = "02_happy_nimbus_within_tolerance.pdf"


def decide():
    db.reset()
    rec = pipeline.run_sync(SAMPLE, (config.SAMPLES_DIR / SAMPLE).read_bytes())
    return rec["decision"]["status"], rec


def main():
    checks = {}
    try:
        config.set_policy({"tolerance_pct": 0})
        status, rec = decide()
        checks["0% tolerance holds the ₹640 usage variance"] = status == "NEEDS_REVIEW"
        checks["run records the policy it was decided under"] = rec["policy"]["tolerance_pct"] == 0

        config.reset_policy()
        status, _ = decide()
        checks["default 2% tolerance approves it"] = status == "APPROVED"

        for bad in ({"tolerance_pct": 11}, {"tolerance_cap": -1}, {"duplicate_window_days": 0},
                    {"duplicate_window_days": 2.5}, {"tolerance_pct": "abc"}):
            try:
                config.set_policy(bad)
                checks[f"rejects {bad}"] = False
            except ValueError:
                checks[f"rejects {bad}"] = True
        checks["invalid input leaves policy unchanged"] = config.get_policy() == config.DEFAULT_POLICY
    finally:
        config.reset_policy()

    for name, ok in checks.items():
        print(("PASS  " if ok else "FAIL  ") + name)
    print("\nPOLICY OK" if all(checks.values()) else "\nPOLICY FAILURES")
    return all(checks.values())


def test_policy():
    assert main()


if __name__ == "__main__":
    main()

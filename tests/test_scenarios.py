"""Runs every sample invoice through the full pipeline (in demo order, from a
clean ledger) and checks the decision matches the scenario's expectation.

    python -m tests.test_scenarios          (or: pytest tests/)
"""
import json
import os
import tempfile

os.environ.setdefault("STAGE_DELAY_MS", "0")
if os.environ.get("CLEARAP_TEST_LIVE_AI") != "1":  # offline by default: no API calls, no quota used
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.setdefault("CLEARAP_TODAY", "2026-11-02")
os.environ.setdefault("CLEARAP_DATA_DIR", tempfile.mkdtemp(prefix="clearap-test-"))

from app import config, db, pipeline  # noqa: E402


def run_all(verbose=True):
    db.reset()
    manifest = json.loads((config.SAMPLES_DIR / "manifest.json").read_text())
    failures = []
    for s in manifest:
        rec = pipeline.run_sync(s["file"], (config.SAMPLES_DIR / s["file"]).read_bytes(), "test")
        got = rec["decision"]["status"]
        ok = got == s["expect"]
        if verbose:
            print(f"{'PASS' if ok else 'FAIL'}  {s['file']:<42} expected {s['expect']:<13} got {got:<13} {rec['decision'].get('headline')}")
            for i in rec["decision"].get("issues", []):
                print(f"        - [{i['outcome']}] {i['name']}: {i['message'][:150]}")
        if not ok:
            failures.append((s["file"], s["expect"], got))
    return failures


def test_all_scenarios():
    assert run_all(verbose=False) == []


if __name__ == "__main__":
    fails = run_all()
    print("\nALL SCENARIOS PASS" if not fails else f"\n{len(fails)} FAILURE(S): {fails}")

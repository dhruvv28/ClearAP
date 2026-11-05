"""Exercises the LLM path without network access, using a simulated model that
behaves like a real one: title-case names, numbers as strings with commas,
'1 Lot' quantities, extra wording, and (in 'adversarial' mode) a misread total.

    python -m tests.test_ai_path
"""
import json
import os
import random
import tempfile

os.environ["STAGE_DELAY_MS"] = "0"
os.environ.setdefault("CLEARAP_TODAY", "2026-11-02")
os.environ["CLEARAP_DATA_DIR"] = tempfile.mkdtemp(prefix="clearap-ai-")
os.environ["GEMINI_API_KEY"] = "simulated"

import httpx  # noqa: E402

from app import config, db, extract, pipeline  # noqa: E402

MODE = {"misread_total": False, "fail": False}


def simulated_model(text: str) -> dict:
    f = extract.heuristic_parse(text)["fields"]
    rnd = random.Random(len(text))
    fmt = lambda x: f"{x:,.2f}" if isinstance(x, float) and rnd.random() < 0.5 else x  # noqa: E731
    d = {k: f.get(k) for k in extract.SCALAR_FIELDS}
    if d.get("vendor_name"):
        d["vendor_name"] = d["vendor_name"].title() + (" Pvt Ltd" if "pvt" not in d["vendor_name"].lower() else "")
    if d.get("bank_name"):
        d["bank_name"] = d["bank_name"] + " Ltd"
    for k in ("subtotal", "tax_total", "total"):
        d[k] = fmt(d[k])
    if MODE["misread_total"] and f.get("total"):
        d["total"] = round(f["total"] + 9000, 2)   # e.g. a 1 read as a 9 on a scan
    d["line_items"] = [{"description": li["description"],
                        "quantity": f"{li['quantity']:g} Lot" if rnd.random() < 0.3 else li["quantity"],
                        "unit_price": fmt(li["unit_price"]), "amount": fmt(li["amount"])} for li in f["line_items"]]
    d["tax_lines"] = f["tax_lines"]
    d["tax_inclusive"] = f["tax_inclusive"]
    d["bank_change_notice"] = f["bank_change_notice"]
    d["confidence"] = {k: 0.93 for k in extract.SCALAR_FIELDS}
    return d


def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
    if MODE["fail"]:
        raise httpx.ConnectTimeout("simulated timeout")
    prompt = json["contents"][0]["parts"][-1]["text"]
    if "<<<" in prompt:
        out = __import__("json").dumps(simulated_model(prompt.split("<<<", 1)[1]))
    else:
        out = "Summary rewritten by the simulated model."
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": out}]}}]},
                          request=httpx.Request("POST", url))


def run(label):
    db.reset()
    bad = []
    for s in json.loads((config.SAMPLES_DIR / "manifest.json").read_text()):
        rec = pipeline.run_sync(s["file"], (config.SAMPLES_DIR / s["file"]).read_bytes())
        if rec["decision"]["status"] != s["expect"]:
            bad.append((s["file"], s["expect"], rec["decision"]["status"],
                        [i["message"][:140] for i in rec["decision"]["issues"]]))
    print(f"{label:<34} {'ALL PASS' if not bad else bad}")
    return rec, bad


if __name__ == "__main__":
    httpx.post = fake_post
    _, b1 = run("Simulated Gemini, realistic output")
    MODE["misread_total"] = True
    rec, _ = run("Simulated Gemini, misreads totals")
    print("   e.g. last run extraction check:",
          [c["message"][:150] for c in rec["checks"] if c["id"] == "extraction_confidence"])
    MODE.update(misread_total=False, fail=True)
    _, b3 = run("Gemini down (timeouts)")
    print("\nAI PATH OK" if not (b1 or b3) else "\nAI PATH FAILURES")

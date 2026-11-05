"""HTTP layer: upload, live run stream (SSE), dashboard data, reference data."""
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, extract, llm, pipeline

app = FastAPI(title="ClearAP: invoice to decision")


@app.middleware("http")
async def no_stale_ui(request, call_next):
    """Make browsers re-check the UI files on every load, so an update is never half-applied
    (new page with old styles/scripts). Unchanged files still come back as a cheap 304."""
    response = await call_next(request)
    if request.url.path in ("/", "/index.html", "/app.js", "/app.css"):
        response.headers["Cache-Control"] = "no-cache"
    return response
db.conn()


@app.get("/api/config")
def get_config():
    return {"extraction": f"{llm.display_name()} + rule-based cross-check" if llm.available() else "Rule-based parser + OCR",
            "ai_enabled": llm.available(), "provider": llm.display_name(), "model": llm.model_name(),
            "tolerance_pct": config.TOLERANCE_PCT, "tolerance_cap": config.TOLERANCE_ABS_CAP,
            "duplicate_window_days": config.DUPLICATE_WINDOW_DAYS,
            "min_confidence": config.MIN_FIELD_CONFIDENCE, "buyer": config.BUYER_NAME, "buyer_gstin": config.BUYER_GSTIN,
            "today": config.today().isoformat(), "stages": [{"id": s, "title": t} for s, t in pipeline.STAGES]}


@app.get("/api/samples")
def samples():
    return json.loads((config.SAMPLES_DIR / "manifest.json").read_text())


@app.post("/api/runs")
async def create_run(file: UploadFile | None = File(None), sample: str | None = Form(None),
                     fast: bool = Form(False)):
    if sample:
        p = (config.SAMPLES_DIR / Path(sample).name)
        if not p.exists() or p.suffix.lower() != ".pdf":
            raise HTTPException(404, "Unknown sample")
        data, name, source = p.read_bytes(), p.name, "sample"
    elif file is not None:
        data = await file.read()
        name, source = file.filename or "upload.pdf", "upload"
        if not data[:5].startswith(b"%PDF"):
            raise HTTPException(400, "Please upload a PDF invoice.")
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(413, "File too large (15 MB max).")
    else:
        raise HTTPException(400, "Provide a file or a sample name.")
    run_id = pipeline.start_run(name, data, source, fast=fast)
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    if run_id not in pipeline.RUNS:
        rec = db.get_run(run_id)
        if not rec:
            raise HTTPException(404, "Unknown run")

        async def replay():
            yield f"data: {json.dumps({'type': 'done', 'run_id': run_id, 'record': rec}, default=str)}\n\n"
        return StreamingResponse(replay(), media_type="text/event-stream")

    async def stream():
        sent = 0
        buf = pipeline.RUNS[run_id]
        while True:
            events = buf["events"]
            while sent < len(events):
                yield f"data: {json.dumps(events[sent], default=str)}\n\n"
                sent += 1
            if buf["done"] and sent >= len(buf["events"]):
                break
            await asyncio.sleep(0.08)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def runs():
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def run(run_id: str):
    rec = db.get_run(run_id)
    if not rec:
        raise HTTPException(404, "Unknown run")
    return rec


class ReviewIn(BaseModel):
    action: str
    note: str = ""
    reviewer: str = ""


@app.post("/api/runs/{run_id}/review")
def review(run_id: str, body: ReviewIn):
    try:
        return pipeline.apply_review(run_id, body.action, body.note, body.reviewer)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/runs/{run_id}/audit.json")
def run_download(run_id: str):
    rec = db.get_run(run_id)
    if not rec:
        raise HTTPException(404, "Unknown run")
    rec = {k: v for k, v in rec.items() if k != "events"}
    return JSONResponse(rec, headers={"Content-Disposition": f'attachment; filename="clearap-{run_id}.json"'})


@app.get("/api/runs.csv")
def runs_csv():
    import csv
    import io
    cols = ["id", "created_at", "filename", "vendor", "invoice_number", "total", "po_number", "decision", "reason",
            "owner", "review_status", "duration_ms"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["run_id"] + cols[1:])
    for r in db.list_runs():
        w.writerow([r.get(c) if r.get(c) is not None else "" for c in cols])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="clearap-runs-{config.today().isoformat()}.csv"'})


class PolicyIn(BaseModel):
    tolerance_pct: float | None = None
    tolerance_cap: float | None = None
    duplicate_window_days: float | None = None


@app.get("/api/policy")
def policy():
    return {"policy": config.get_policy(), "defaults": config.DEFAULT_POLICY, "limits": config.POLICY_LIMITS}


@app.put("/api/policy")
def update_policy(body: PolicyIn):
    try:
        p = config.set_policy({k: v for k, v in body.model_dump().items() if v is not None})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"policy": p, "defaults": config.DEFAULT_POLICY, "limits": config.POLICY_LIMITS}


@app.delete("/api/policy")
def restore_policy():
    return {"policy": config.reset_policy(), "defaults": config.DEFAULT_POLICY, "limits": config.POLICY_LIMITS}


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str):
    p = config.UPLOAD_DIR / f"{Path(run_id).name}.pdf"
    if not p.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(p, media_type="application/pdf")


@app.get("/api/stats")
def stats():
    rs = db.list_runs()

    def final(r):
        if r.get("review_status") == "RELEASED":
            return "RELEASED"
        if r.get("review_status") == "REJECTED_BY_REVIEWER":
            return "REJECTED"
        return r["decision"]

    auto = [r for r in rs if r["decision"] == "APPROVED"]
    released = [r for r in rs if final(r) == "RELEASED"]
    waiting = [r for r in rs if final(r) == "NEEDS_REVIEW"]
    stopped = [r for r in rs if final(r) == "REJECTED"]
    reasons: dict[str, int] = {}
    for r in rs:
        if r["decision"] in ("NEEDS_REVIEW", "REJECTED"):
            reasons[r["reason"] or "Other"] = reasons.get(r["reason"] or "Other", 0) + 1
    tot = lambda xs: round(sum(x["total"] or 0 for x in xs), 2)  # noqa: E731
    return {"runs": len(rs), "approved": len(auto), "released": len(released), "review": len(waiting),
            "rejected": len(stopped), "value_approved": tot(auto + released), "value_held": tot(waiting),
            "value_blocked": tot(stopped),
            "touchless_rate": round(len(auto) / len(rs), 3) if rs else None,
            "avg_ms": round(sum(r["duration_ms"] or 0 for r in rs) / len(rs)) if rs else None,
            "reasons": sorted(reasons.items(), key=lambda x: -x[1])}


@app.get("/api/reference")
def reference():
    pos = []
    for p in db.POS:
        billed, prior = db.billed_against_po(p["po_number"])
        pos.append({**{k: p[k] for k in ("po_number", "vendor_id", "issue_date", "description", "subtotal", "tax_rate",
                                         "total", "status", "blanket", "lines")},
                    "vendor": db.VENDOR_BY_ID[p["vendor_id"]]["name"], "billed": billed,
                    "remaining": round(p["subtotal"] - billed, 2), "invoices": [i["invoice_number"] for i in prior]})
    vendors = [{k: v[k] for k in ("vendor_id", "name", "gstin", "state", "bank_name", "ifsc", "payment_terms_days",
                                  "status", "status_note")} | {"bank_last4": v["bank_account"][-4:]} for v in db.VENDORS]
    return {"vendors": vendors, "pos": pos, "ledger": db.all_ledger()}


@app.post("/api/reset")
def reset():
    db.reset()
    config.reset_policy()
    pipeline.RUNS.clear()
    return {"ok": True}


@app.get("/healthz")
def health():
    return {"ok": True}


app.mount("/samples", StaticFiles(directory=config.SAMPLES_DIR), name="samples")
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")


@app.exception_handler(Exception)
async def unhandled(_, exc):
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

"""Storage: reference data (vendor master, POs) from CSV, plus a SQLite ledger and run log.

The ledger is the 'procurement system' side: every invoice that has been paid,
approved or put on hold. Duplicate detection and PO balance tracking read it.
"""
import csv
import json
import sqlite3
import threading
from datetime import datetime

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


# ---------------------------------------------------------------- reference data
def _read_csv(name):
    with open(config.REF_DIR / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_vendors() -> list[dict]:
    rows = _read_csv("vendors.csv")
    for r in rows:
        r["aliases"] = [a for a in r["aliases"].split("|") if a]
        r["payment_terms_days"] = int(r["payment_terms_days"])
    return rows


def load_pos() -> list[dict]:
    pos = _read_csv("purchase_orders.csv")
    lines = _read_csv("po_lines.csv")
    for p in pos:
        for k in ("subtotal", "tax_rate", "total"):
            p[k] = float(p[k])
        p["blanket"] = "blanket" in p["description"].lower()
        p["lines"] = [
            {"line_no": int(l["line_no"]), "item": l["item"], "quantity": float(l["quantity"]),
             "unit_price": float(l["unit_price"])}
            for l in lines if l["po_number"] == p["po_number"]
        ]
    return pos


VENDORS = load_vendors()
POS = load_pos()
VENDOR_BY_ID = {v["vendor_id"]: v for v in VENDORS}
PO_BY_NUMBER = {p["po_number"]: p for p in POS}


# ---------------------------------------------------------------- sqlite
SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vendor_id TEXT, invoice_number TEXT, invoice_number_norm TEXT, invoice_date TEXT,
  po_number TEXT, subtotal REAL, tax REAL, total REAL,
  status TEXT,            -- paid | approved | on_hold
  source TEXT,            -- history | run
  run_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, created_at TEXT, filename TEXT, source TEXT,
  decision TEXT, vendor TEXT, invoice_number TEXT, total REAL, po_number TEXT,
  reason TEXT, owner TEXT, extraction_mode TEXT, duration_ms INTEGER, record TEXT
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            fresh = not config.DB_PATH.exists()
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            try:  # older local databases: add the reviewer column
                _conn.execute("ALTER TABLE runs ADD COLUMN review_status TEXT")
            except sqlite3.OperationalError:
                pass
            if fresh:
                _seed_history()
        return _conn


def _seed_history():
    from .normalize import norm_invoice_number
    c = _conn
    for r in _read_csv("invoice_history.csv"):
        c.execute(
            "INSERT INTO ledger (vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number,"
            " subtotal, tax, total, status, source, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["vendor_id"], r["invoice_number"], norm_invoice_number(r["invoice_number"]), r["invoice_date"],
             r["po_number"] or None, float(r["subtotal"]), float(r["tax"]), float(r["total"]), r["status"],
             "history", None, config.now_iso()))
    c.commit()


def reset():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        for f in config.UPLOAD_DIR.glob("*") if config.UPLOAD_DIR.exists() else []:
            f.unlink()
        conn()


def ledger_for_vendor(vendor_id: str) -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT * FROM ledger WHERE vendor_id=? ORDER BY invoice_date", (vendor_id,)).fetchall()
    return [dict(r) for r in rows]


def ledger_for_po(po_number: str) -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT * FROM ledger WHERE po_number=? ORDER BY invoice_date", (po_number,)).fetchall()
    return [dict(r) for r in rows]


def po_status(po: dict) -> str:
    """Live status: a PO that's open in the register but fully invoiced counts as 'fully billed'."""
    if po["status"] != "open":
        return po["status"]
    billed, _ = billed_against_po(po["po_number"])
    return "fully billed" if billed >= po["subtotal"] - 0.5 else "open"


def billed_against_po(po_number: str) -> tuple[float, list[dict]]:
    """Subtotal already consumed on a PO (paid + approved; holds don't consume)."""
    rows = [r for r in ledger_for_po(po_number) if r["status"] in ("paid", "approved")]
    return round(sum(r["subtotal"] or 0 for r in rows), 2), rows


def add_ledger(entry: dict):
    with _lock:
        conn().execute(
            "INSERT INTO ledger (vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number, subtotal,"
            " tax, total, status, source, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry["vendor_id"], entry.get("invoice_number"), entry.get("invoice_number_norm"),
             entry.get("invoice_date"), entry.get("po_number"), entry.get("subtotal"), entry.get("tax"),
             entry.get("total"), entry["status"], "run", entry.get("run_id"),
             config.now_iso()))
        conn().commit()


def all_ledger() -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT * FROM ledger ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def save_run(rec: dict):
    d = rec.get("decision") or {}
    inv = rec.get("invoice") or {}
    with _lock:
        conn().execute(
            "INSERT OR REPLACE INTO runs (id, created_at, filename, source, decision, vendor, invoice_number, total,"
            " po_number, reason, owner, extraction_mode, duration_ms, record) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], rec["created_at"], rec["filename"], rec.get("source"), d.get("status"),
             (rec.get("vendor") or {}).get("name") or inv.get("vendor_name"), inv.get("invoice_number"),
             inv.get("total"), (rec.get("po") or {}).get("po_number"), d.get("headline"), d.get("owner"),
             rec.get("extraction_mode"), rec.get("duration_ms"), json.dumps(rec, default=str)))
        conn().commit()


def list_runs() -> list[dict]:
    with _lock:
        rows = conn().execute(
            "SELECT id, created_at, filename, source, decision, vendor, invoice_number, total, po_number, reason,"
            " owner, extraction_mode, duration_ms, review_status FROM runs ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def update_ledger_status(run_id: str, status: str) -> int:
    with _lock:
        cur = conn().execute("UPDATE ledger SET status=? WHERE run_id=?", (status, run_id))
        conn().commit()
        return cur.rowcount


def save_review(rec: dict, review_status: str):
    with _lock:
        conn().execute("UPDATE runs SET record=?, review_status=? WHERE id=?",
                       (json.dumps(rec, default=str), review_status, rec["id"]))
        conn().commit()


def get_run(run_id: str) -> dict | None:
    with _lock:
        row = conn().execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone()
    return json.loads(row["record"]) if row else None

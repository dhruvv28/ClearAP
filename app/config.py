"""Central configuration. Everything a finance controller would want to tune lives here."""
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CLEARAP_DATA_DIR", ROOT / "runtime"))
REF_DIR = ROOT / "data"
SAMPLES_DIR = ROOT / "samples"
STATIC_DIR = ROOT / "static"
DB_PATH = DATA_DIR / "clearap.db"
UPLOAD_DIR = DATA_DIR / "uploads"

# --- AI extraction -------------------------------------------------------
# Gemini is preferred when its key is set (Google AI Studio has a free tier); Claude otherwise.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
# Free-tier quotas are per model. If the main model is rate-limited, try this lighter one before
# falling back to the rule-based reader. Set to "" to disable.
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
ANTHROPIC_WORKSPACE_ID = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()  # only for keys not scoped to a workspace


def anthropic_headers() -> dict:
    h = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    if ANTHROPIC_WORKSPACE_ID:
        h["anthropic-workspace-id"] = ANTHROPIC_WORKSPACE_ID
    return h
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "25"))
# Optional: let the LLM reword the decision summary. Off by default: it adds a second model call per
# invoice (slower, and twice the free-tier quota) for a cosmetic gain. The rule-built summary is complete.
LLM_POLISH = os.environ.get("LLM_POLISH", "0") == "1"

# --- Buyer (us) ------------------------------------------------------------
BUYER_NAME = "Meridian Retail Pvt Ltd"
BUYER_GSTIN = "29AAACM1234F1ZU"
BUYER_STATE_CODE = BUYER_GSTIN[:2]

# --- Matching policy ---------------------------------------------------------
TOLERANCE_PCT = float(os.environ.get("TOLERANCE_PCT", "2.0"))       # % of PO value
TOLERANCE_ABS_CAP = float(os.environ.get("TOLERANCE_ABS_CAP", "25000"))  # never more than this many rupees
TAX_RATE_TOLERANCE_PP = 0.5          # percentage points
MIN_FIELD_CONFIDENCE = 0.70          # below this a critical field goes to a human
PO_INFERENCE_MIN_SCORE = 0.80        # inferred PO must score at least this...
PO_INFERENCE_MIN_MARGIN = 0.20       # ...and beat the runner-up by this much
DUPLICATE_WINDOW_DAYS = 45
STALE_INVOICE_DAYS = 180

# Demo clock: the case study is set in early November 2026.
_FIXED_TODAY = os.environ.get("CLEARAP_TODAY")


def today() -> date:
    """The business date. Pinned by CLEARAP_TODAY (tests), otherwise the real date at call time."""
    return date.fromisoformat(_FIXED_TODAY) if _FIXED_TODAY else date.today()


def now_iso() -> str:
    """Timestamp with its UTC offset, so browsers in any timezone show the right local time."""
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")

# Small pause between stages so the live view is readable during a demo.
STAGE_DELAY_MS = int(os.environ.get("STAGE_DELAY_MS", "450"))


# ---- Editable policy (in memory; a restart or "Reset demo" restores the defaults) ----
DEFAULT_POLICY = {"tolerance_pct": TOLERANCE_PCT, "tolerance_cap": TOLERANCE_ABS_CAP,
                  "duplicate_window_days": DUPLICATE_WINDOW_DAYS}
POLICY_LIMITS = {"tolerance_pct": (0.0, 10.0), "tolerance_cap": (0.0, 100000.0), "duplicate_window_days": (1, 365)}


def get_policy() -> dict:
    return {"tolerance_pct": TOLERANCE_PCT, "tolerance_cap": TOLERANCE_ABS_CAP,
            "duplicate_window_days": DUPLICATE_WINDOW_DAYS}


def set_policy(values: dict) -> dict:
    """Validate everything first, then apply all-or-nothing."""
    global TOLERANCE_PCT, TOLERANCE_ABS_CAP, DUPLICATE_WINDOW_DAYS
    labels = {"tolerance_pct": "Tolerance %", "tolerance_cap": "Tolerance cap", "duplicate_window_days": "Duplicate window"}
    clean = get_policy()
    for k, v in values.items():
        if k not in POLICY_LIMITS:
            raise ValueError(f"Unknown setting: {k}")
        try:
            num = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{labels[k]} must be a number.")
        lo, hi = POLICY_LIMITS[k]
        if not lo <= num <= hi:
            raise ValueError(f"{labels[k]} must be between {lo:g} and {hi:,.0f}.")
        if k == "duplicate_window_days":
            if num != int(num):
                raise ValueError("Duplicate window must be a whole number of days.")
            num = int(num)
        clean[k] = num
    TOLERANCE_PCT, TOLERANCE_ABS_CAP = clean["tolerance_pct"], clean["tolerance_cap"]
    DUPLICATE_WINDOW_DAYS = clean["duplicate_window_days"]
    return get_policy()


def reset_policy() -> dict:
    return set_policy(DEFAULT_POLICY)


def policy_text(p: dict | None = None) -> str:
    p = p or get_policy()
    return (f"{p['tolerance_pct']:g}% tolerance (capped at Rs. {p['tolerance_cap']:,.0f}), "
            f"{p['duplicate_window_days']}-day duplicate window")


def tolerance_for(po_value: float) -> float:
    return min(po_value * TOLERANCE_PCT / 100.0, TOLERANCE_ABS_CAP)

"""GSTIN format + checksum validation (Indian GST registration number).

A GSTIN is 15 characters: 2-digit state code, 10-char PAN, entity number,
the letter Z, and a base-36 check character. The checksum catches most
single-character OCR misreads (e.g. 0 vs O, 8 vs B), which is why the
pipeline validates it instead of trusting the extracted string.
"""
import re

CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

STATE_CODES = {
    "27": "Maharashtra", "29": "Karnataka", "33": "Tamil Nadu",
    "36": "Telangana", "07": "Delhi", "32": "Kerala", "24": "Gujarat",
}


def checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        factor = 1 if i % 2 == 0 else 2
        product = CHARSET.index(ch) * factor
        total += product // 36 + product % 36
    return CHARSET[(36 - total % 36) % 36]


def make_gstin(first14: str) -> str:
    return first14 + checksum_char(first14)


def validate(gstin: str | None) -> tuple[bool, str]:
    if not gstin:
        return False, "missing"
    g = gstin.strip().upper()
    if not GSTIN_RE.match(g):
        return False, "bad format"
    if checksum_char(g[:14]) != g[14]:
        return False, "checksum failed"
    return True, "valid"

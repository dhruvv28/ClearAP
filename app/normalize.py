"""Small, boring, well-tested helpers: amounts, dates, invoice numbers, text similarity."""
import re
from datetime import date, datetime
from difflib import SequenceMatcher

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_amount(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace("₹", "").replace("INR", "").replace("Rs.", "").replace("Rs", "")
    t = t.replace(",", "").replace(" ", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def parse_date(s) -> str | None:
    """Return ISO yyyy-mm-dd. Indian invoices are day-first (dd/mm/yyyy)."""
    if not s:
        return None
    s = str(s).strip().rstrip(".,")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", s)
        if m:
            d, mo, y = map(int, m.groups())
            y = y + 2000 if y < 100 else y
        else:
            m = re.match(r"^(\d{1,2})[\s\-]+([A-Za-z]{3,9})[\s\-,]+(\d{4})$", s)
            if not m:
                return None
            d, mon, y = int(m.group(1)), m.group(2)[:3].lower(), int(m.group(3))
            mo = MONTHS.get(mon)
            if not mo:
                return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def norm_invoice_number(s: str | None) -> str | None:
    """Canonical form for duplicate detection: upper-case alphanumerics only.

    'KOS/26-27/0187' -> 'KOS26270187'.  Format variants ('KOS-2627-187') are
    caught separately by the fuzzy rule, which compares digit sequences.
    """
    if not s:
        return None
    return re.sub(r"[^A-Z0-9]", "", s.upper()) or None


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


STOP = {"the", "of", "and", "for", "to", "a", "in", "as", "per", "with", "by", "-", "&"}


def tokens(s: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (s or "").lower())
    return {w for w in words if w not in STOP and len(w) > 1}


def text_similarity(a: str, b: str) -> float:
    """Blend of token overlap and character similarity, 0..1."""
    ta, tb = tokens(a), tokens(b)
    jacc = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    contain = len(ta & tb) / min(len(ta), len(tb)) if ta and tb else 0.0
    char = SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    return round(max(jacc, 0.6 * contain + 0.4 * char), 3)


def days_between(a: str, b: str) -> int | None:
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).days)
    except (TypeError, ValueError):
        return None


def fmt_inr(x) -> str:
    if x is None:
        return "—"
    neg = x < 0
    x = abs(float(x))
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return ("-" if neg else "") + "₹" + whole + ("" if frac == "00" else "." + frac)

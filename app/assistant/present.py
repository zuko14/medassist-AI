"""Kriya Ketu R0 result cards (ux-design-spec table card; KK-P4-01). Pure: response JSON in, cards out.

Results are stored in assistant_runs.result, which may hold PHI, so a card keeps
at most MAX_ROWS rows (the widget shows 8 and links to the page for the rest) and
phone numbers are masked (security-and-threat-model, "Previews / results").
Only the fields a card names are copied, so nothing else in a response (e.g. an
error string) is ever stored.
"""

from datetime import date, datetime, timezone

from app.utils.validators import mask_phone
from app.voice.dates import IST

MAX_ROWS = 8


def at(data, key: str):
    """data["a"]["b"] for key "a.b"; None when any part is missing."""
    for part in key.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data


def fmt(value, kind: str = "text") -> str:
    if value is None or value == "":
        return "—"
    try:
        if kind == "phone":
            return mask_phone(str(value))
        if kind == "paise":
            return f"₹{int(value) / 100:,.0f}"
        if kind == "rupees":
            return f"₹{float(value):,.0f}"
        if kind == "pct":
            return f"{value}%"
        if kind == "yesno":
            return "Yes" if value else "No"
        if kind == "label":
            return str(value).replace("_", " ").capitalize()[:80]
        if kind == "time":
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(IST).strftime("%d %b, %H:%M")
        if kind == "date":
            return date.fromisoformat(str(value)[:10]).strftime("%d %b")
    except (TypeError, ValueError, OverflowError):
        return "—"
    return str(value)[:80]


def _note(text: str, page) -> dict:
    return {"message": text, "navigate": None, "cards": [{"type": "help", "text": text, "page": page}]}


def table(title: str, page, columns: tuple, rows, total=None) -> dict:
    """columns: (header, key, kind) each. total: the endpoint's own count when it pages."""
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    total = total if isinstance(total, int) and not isinstance(total, bool) else len(rows)
    if not rows:
        return _note(f"{title}: nothing to show.", page)
    card = {"type": "table", "title": f"{title} ({total})", "columns": [c[0] for c in columns],
            "rows": [[fmt(at(r, key), kind) for _, key, kind in columns] for r in rows[:MAX_ROWS]],
            "total": total, "page": page}
    return {"message": f"{title}: {total}.", "navigate": None, "cards": [card]}


def metrics(title: str, page, data, fields: tuple) -> dict:
    """fields: (label, key, kind) each; a field the response lacks is left out."""
    rows = [[label, fmt(value, kind)] for label, key, kind in fields if (value := at(data, key)) is not None]
    if not rows:
        return _note(f"{title}: no figures to show.", page)
    card = {"type": "table", "title": title, "columns": ["Measure", "Value"], "rows": rows,
            "total": len(rows), "page": page}
    return {"message": title + ".", "navigate": None, "cards": [card]}

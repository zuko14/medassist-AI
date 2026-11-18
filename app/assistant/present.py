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


def _units(store: dict, unit: str) -> str:
    pl = store.get("packs_loose") or {}
    packs, loose, total = pl.get("packs"), pl.get("loose"), store.get("qty_units")
    if packs is None:
        return f"{total} {unit}"
    if not loose:
        return f"{packs} strips ({total} {unit})" if packs else f"0 {unit}"
    return f"{packs} strips + {loose} {unit} ({total} {unit})" if packs else f"{loose} {unit}"


def rx_stock(data, params=None) -> dict:
    """KRIYA-KETU §2.1, from GET /admin/rx/stock/answer. Same-generic items are listed only as information:
    no text here ever says "use instead", "can be given" or anything about dosing."""
    data = data if isinstance(data, dict) else {}
    status, matches = data.get("status"), data.get("matches") or []
    q = str(data.get("query") or "that")[:60]
    if status == "not_found" or not matches:
        return _note(f'No medicine matching "{q}" in the drug master.', "rxitems")
    if status == "ambiguous":
        names = [str(at(m, "item.brand_name")) for m in matches[:3] if at(m, "item.brand_name")]
        card = {"type": "table", "title": "Did you mean…?", "columns": ["Medicine", "Code"],
                "rows": [[fmt(at(m, "item.brand_name")), fmt(at(m, "item.item_code"))] for m in matches[:3]],
                "total": len(names), "page": "rxstock"}
        return {"message": "Did you mean " + " or ".join(names) + "? Ask again with the full name.",
                "navigate": None, "cards": [card]}
    m = matches[0]
    item, gen = m.get("item") or {}, m.get("generic") or {}
    unit = item.get("base_unit") or "units"
    name = item.get("brand_name") or q
    comp = " ".join(str(x) for x in (gen.get("name"), gen.get("strength"), gen.get("dosage_form")) if x)
    stores = [s for s in m.get("stores") or [] if isinstance(s, dict)]
    rows = [[fmt(s.get("store_name")), _units(s, unit), fmt(s.get("nearest_expiry"), "date")] for s in stores[:MAX_ROWS]]
    if "available_at_pharmacy" in m:
        rows.append(["Pharmacy", "Available" if m["available_at_pharmacy"] else "Not available", "—"])
    subs = [s for s in m.get("substitutes_in_stock") or [] if isinstance(s, dict)]
    same = ", ".join(f"{s.get('brand_name')}" + (f" ({s['qty_units']} {unit})" if s.get("qty_units") is not None else "")
                     for s in subs[:5])
    in_stock = status == "match"
    if in_stock:
        where = "; ".join(f"{s.get('store_name')}: {_units(s, unit)}" for s in stores if s.get("qty_units"))
        message = f"{name}{f' ({comp})' if comp else ''} — " + (where or "available at the pharmacy") + "."
    else:
        message = f"{name} is out of stock" + (f" at {stores[0].get('store_name')} (0)" if len(stores) == 1 else "") + "."
        if same:
            message += f" Same composition in stock: {same}."
    cards = [{"type": "table", "title": name + (f" · {comp}" if comp else ""), "columns": ["Store", "In stock", "Nearest expiry"],
              "rows": rows or [["—", "0", "—"]], "total": len(rows), "page": "rxstock"}]
    if same and in_stock:
        cards.append({"type": "help", "text": f"Same composition in stock: {same}.", "page": None})
    cards.append({"type": "help", "text": "To bill it, open the Pharmacy POS.", "page": "rxpos"})
    return {"message": message, "navigate": None, "cards": cards}

"""Kriya Pharmacy OS — drug master (migration 135, RX-13…RX-15).

Manufacturers, generics, items, barcodes; the CSV/XLSX item import (validated here,
applied atomically by SQL rx_import_items); the deterministic resolver
(SQL rx_resolve_items) and same-generic substitutes.
Design: docs/pharmacy-os/DATA-MODEL.md §2, API.md §2, ROLLOUT.md §4.2.

Zero-LLM (ADR-RX-06): matching is barcode / confirmed alias / code / substring, and a
substitute is only ever another active item of the same generic_id.
"""

import csv
import io
from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.database import sb, scoped_query, supabase
from app.services.rx import _rows, explain, raise_for

ItemKind = Literal["drug", "consumable", "surgical", "otc", "nutrition", "device"]
Schedule = Literal["none", "G", "H", "H1", "X", "narcotic", "psychotropic"]
BaseUnit = Literal["tablet", "capsule", "ml", "gram", "vial", "ampoule", "bottle", "tube", "sachet", "piece", "pair",
                   "box", "inhaler", "patch"]
PackLabel = Literal["strip", "blister", "bottle", "vial", "ampoule", "tube", "box", "sachet", "piece", "pack"]
DosageForm = Literal["tablet", "capsule", "syrup", "suspension", "injection", "drops", "ointment", "cream", "gel",
                     "inhaler", "powder", "lotion", "spray", "patch", "infusion", "other"]
Route = Literal["oral", "topical", "iv", "im", "sc", "inhalation", "nasal", "ophthalmic", "otic", "rectal", "vaginal",
                "sublingual", "other"]
Symbology = Literal["ean13", "ean8", "gs1_128", "gs1_datamatrix", "upc", "internal"]
TaxBps = Literal[0, 500, 1200, 1800, 2800, 4000]

ITEM_CODE = r"^[A-Z0-9_-]{2,40}$"
HSN = r"^[0-9]{4,8}$"
BARCODE = r"^[0-9A-Za-z]{6,40}$"
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 20000


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ManufacturerIn(_In):
    name: str = Field(..., min_length=2, max_length=120)


class GenericIn(_In):
    name: str = Field(..., min_length=2, max_length=200)
    strength: str = Field(..., min_length=1, max_length=60)
    dosage_form: DosageForm
    route: Route = "oral"


class ItemIn(_In):
    item_code: str = Field(..., pattern=ITEM_CODE)
    brand_name: str = Field(..., min_length=2, max_length=200)
    generic_id: Optional[str] = None
    manufacturer_id: Optional[str] = None
    item_kind: ItemKind = "drug"
    schedule: Schedule = "none"
    base_unit: BaseUnit
    pack_size: int = Field(..., ge=1, le=10000)
    pack_label: PackLabel = "strip"
    allow_loose: bool = True
    hsn_code: str = Field(..., pattern=HSN)
    tax_rate_bps: TaxBps
    reorder_level_units: int = Field(0, ge=0, le=10_000_000)
    reorder_qty_units: int = Field(0, ge=0, le=10_000_000)
    ved_class: Optional[Literal["V", "E", "D"]] = None
    is_chronic: bool = False
    is_cold_chain: bool = False
    rack_location: Optional[str] = Field(None, max_length=40)


class ItemPatch(_In):
    """Immutables (item_code, base_unit) are accepted so the database guard answers 409 with the field."""
    item_code: Optional[str] = None
    brand_name: Optional[str] = Field(None, min_length=2, max_length=200)
    generic_id: Optional[str] = None
    manufacturer_id: Optional[str] = None
    item_kind: Optional[ItemKind] = None
    schedule: Optional[Schedule] = None
    base_unit: Optional[BaseUnit] = None
    pack_size: Optional[int] = Field(None, ge=1, le=10000)
    pack_label: Optional[PackLabel] = None
    allow_loose: Optional[bool] = None
    hsn_code: Optional[str] = Field(None, pattern=HSN)
    tax_rate_bps: Optional[TaxBps] = None
    reorder_level_units: Optional[int] = Field(None, ge=0, le=10_000_000)
    reorder_qty_units: Optional[int] = Field(None, ge=0, le=10_000_000)
    ved_class: Optional[Literal["V", "E", "D"]] = None
    is_chronic: Optional[bool] = None
    is_cold_chain: Optional[bool] = None
    rack_location: Optional[str] = Field(None, max_length=40)
    is_active: Optional[bool] = None
    reason: Optional[str] = Field(None, max_length=300)


class BarcodeIn(_In):
    barcode: str = Field(..., pattern=BARCODE)
    symbology: Optional[Symbology] = None


def symbology_for(barcode: str) -> str:
    if barcode.isdigit():
        return {13: "ean13", 8: "ean8", 12: "upc"}.get(len(barcode), "internal")
    return "internal"


# ── masters ─────────────────────────────────────────────────────────────────

async def _insert(table: str, clinic_id: str, row: dict) -> dict:
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table(table).insert({**row, "clinic_id": clinic_id}))
    except Exception as e:
        raise_for(e)
    return res.data[0]


async def list_manufacturers(clinic_id: str) -> list[dict]:
    return await _rows(scoped_query("rx_manufacturers", clinic_id).order("name").limit(1000))


async def create_manufacturer(clinic_id: str, body: ManufacturerIn) -> dict:
    return await _insert("rx_manufacturers", clinic_id, body.model_dump())


async def list_generics(clinic_id: str, q: Optional[str] = None) -> list[dict]:
    query = scoped_query("rx_generics", clinic_id).order("name").limit(500)
    return await _rows(query.ilike("name", f"%{_like(q)}%") if q else query)


async def create_generic(clinic_id: str, body: GenericIn) -> dict:
    return await _insert("rx_generics", clinic_id, body.model_dump())


def _like(q: Optional[str]) -> str:
    return "".join(ch for ch in (q or "").strip() if ch not in "%_*,()\\")[:80]


# ── items ───────────────────────────────────────────────────────────────────

async def list_items(clinic_id: str, q: Optional[str] = None, generic_id: Optional[str] = None,
                     schedule: Optional[str] = None, active: Optional[bool] = None, limit: int = 50) -> list[dict]:
    query = scoped_query("rx_items", clinic_id).order("brand_name").limit(limit)
    if q and _like(q):
        query = query.ilike("brand_name", f"%{_like(q)}%")
    if generic_id:
        query = query.eq("generic_id", generic_id)
    if schedule:
        query = query.eq("schedule", schedule)
    if active is not None:
        query = query.eq("is_active", active)
    items = await _rows(query)
    gids = sorted({i["generic_id"] for i in items if i.get("generic_id")})
    generics = {g["id"]: g for g in await _rows(scoped_query("rx_generics", clinic_id, "id, name, strength, dosage_form")
                                                 .in_("id", gids))} if gids else {}
    for i in items:
        i["generic"] = generics.get(i.get("generic_id"))
    return items


async def get_item(clinic_id: str, item_id: str) -> dict:
    rows = await _rows(scoped_query("rx_items", clinic_id).eq("id", item_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Item not found.")
    return rows[0]


async def item_detail(clinic_id: str, item_id: str) -> dict:
    item = await get_item(clinic_id, item_id)
    by_item = lambda t, sel="*": scoped_query(t, clinic_id, sel).eq("item_id", item_id)  # noqa: E731
    item["barcodes"] = await _rows(by_item("rx_item_barcodes").order("created_at"))
    item["aliases"] = await _rows(by_item("rx_item_aliases").order("alias_norm"))
    item["changes"] = await _rows(by_item("rx_item_changes").order("changed_at", desc=True).limit(50))
    item["generic"] = (await _rows(scoped_query("rx_generics", clinic_id).eq("id", item["generic_id"]).limit(1)) or
                       [None])[0] if item.get("generic_id") else None
    item["manufacturer"] = (await _rows(scoped_query("rx_manufacturers", clinic_id)
                                        .eq("id", item["manufacturer_id"]).limit(1)) or
                            [None])[0] if item.get("manufacturer_id") else None
    return item


async def create_item(clinic_id: str, body: ItemIn) -> dict:
    return await _insert("rx_items", clinic_id, body.model_dump())


async def update_item(clinic_id: str, item_id: str, patch: dict, actor_id: Optional[str], actor_name: str,
                      reason: Optional[str]) -> dict:
    try:
        res = await sb(supabase.rpc("rx_update_item", {"p_clinic_id": clinic_id, "p_item_id": item_id,
                                                       "p_patch": patch, "p_actor_id": actor_id,
                                                       "p_actor_name": actor_name, "p_reason": reason}))
    except Exception as e:
        raise_for(e)
    return res.data


async def add_barcode(clinic_id: str, item_id: str, body: BarcodeIn) -> dict:
    await get_item(clinic_id, item_id)
    return await _insert("rx_item_barcodes", clinic_id, {"item_id": item_id, "barcode": body.barcode,
                                                         "symbology": body.symbology or symbology_for(body.barcode)})


# ── resolver + substitutes (ADR-RX-06/07) ───────────────────────────────────

async def resolve(clinic_id: str, q: str, limit: int = 5) -> list[dict]:
    res = await sb(supabase.rpc("rx_resolve_items", {"p_clinic_id": clinic_id, "p_q": q, "p_limit": limit}))
    return list(res.data or [])


async def substitutes(clinic_id: str, item: dict, store_ids: Optional[list[str]] = None) -> list[dict]:
    """Other active items of the SAME generic (molecule + strength + form). Nothing else, ever."""
    if not item.get("generic_id"):
        return []
    subs = [s for s in await _rows(scoped_query("rx_items", clinic_id, "id, item_code, brand_name, schedule, pack_size, "
                                                "base_unit, manufacturer_id").eq("generic_id", item["generic_id"])
                                   .eq("is_active", True).order("brand_name").limit(50))
            if s["id"] != (item.get("id") or item.get("item_id"))]
    if subs:
        q = scoped_query("rx_v_item_stock", clinic_id, "item_id, sellable_units").in_("item_id", [s["id"] for s in subs])
        stock: dict[str, int] = {}
        for r in await _rows(q.in_("store_id", store_ids) if store_ids is not None else q):
            stock[r["item_id"]] = stock.get(r["item_id"], 0) + int(r["sellable_units"] or 0)
        for s in subs:
            s["stock_units"] = stock.get(s["id"], 0)
    return subs


# ── file import (ROLLOUT §4.2 / §4.3 share the parser) ──────────────────────

def parse_table(raw: bytes, filename: str) -> list[dict]:
    """CSV or XLSX → list of {lower_snake_header: str} (blank cells dropped). Row numbers: data starts at 2."""
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than 5 MB.")
    name = (filename or "").lower()
    if name.endswith(".xlsx") or raw[:2] == b"PK":
        from app.services.price_list_parser import validate_xlsx_zip_safety
        import openpyxl
        try:
            validate_xlsx_zip_safety(raw)
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            grid = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=422, detail="The spreadsheet could not be read. Save it as .xlsx or .csv.")
    else:
        try:
            grid = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        except UnicodeDecodeError:
            raise HTTPException(status_code=422, detail="The CSV must be UTF-8 encoded.")
    if not grid:
        raise HTTPException(status_code=422, detail="The file is empty.")
    header = [str(h or "").strip().lower().replace(" ", "_") for h in grid[0]]
    if len(grid) - 1 > MAX_IMPORT_ROWS:
        raise HTTPException(status_code=413, detail=f"At most {MAX_IMPORT_ROWS} rows per file.")
    out = []
    for n, cells in enumerate(grid[1:], start=2):
        row = {"_row": n}
        for h, v in zip(header, cells):
            if h and v is not None and str(v).strip() != "":
                row[h] = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()
        if len(row) > 1:
            out.append(row)
    return out


class ImportRow(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    item_code: str = Field(..., pattern=ITEM_CODE)
    brand_name: str = Field(..., min_length=2, max_length=200)
    generic_name: Optional[str] = Field(None, min_length=2, max_length=200)
    strength: Optional[str] = Field(None, min_length=1, max_length=60)
    dosage_form: Optional[DosageForm] = None
    route: Optional[Route] = None
    manufacturer: Optional[str] = Field(None, min_length=2, max_length=120)
    item_kind: ItemKind = "drug"
    schedule: Schedule = "none"
    base_unit: BaseUnit
    pack_size: int = Field(..., ge=1, le=10000)
    pack_label: PackLabel = "strip"
    allow_loose: bool = True
    hsn_code: str = Field(..., pattern=HSN)
    gst_rate_percent: Literal["0", "5", "12", "18", "28", "40"]
    reorder_level_units: int = Field(0, ge=0, le=10_000_000)
    reorder_qty_units: int = Field(0, ge=0, le=10_000_000)
    is_chronic: bool = False
    is_cold_chain: bool = False
    rack_location: Optional[str] = Field(None, max_length=40)
    barcode: Optional[str] = Field(None, pattern=BARCODE)

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, v: dict) -> dict:
        v = dict(v)
        if v.get("item_code"):
            v["item_code"] = str(v["item_code"]).strip().upper()
        for k in ("dosage_form", "route", "item_kind", "base_unit", "pack_label"):
            if v.get(k):
                v[k] = str(v[k]).strip().lower()
        if v.get("schedule"):
            s = str(v["schedule"]).strip()
            v["schedule"] = {"none": "none", "g": "G", "h": "H", "h1": "H1", "x": "X", "narcotic": "narcotic",
                             "psychotropic": "psychotropic"}.get(s.lower(), s)
        if v.get("gst_rate_percent"):
            v["gst_rate_percent"] = str(v["gst_rate_percent"]).strip().rstrip("%").removesuffix(".0")
        return v

    @model_validator(mode="after")
    def _generic(self):
        parts = (self.generic_name, self.strength, self.dosage_form)
        if any(parts) and not all(parts):
            raise ValueError("generic_name, strength and dosage_form go together")
        if self.item_kind == "drug" and not all(parts):
            raise ValueError("a drug needs generic_name, strength and dosage_form")
        return self


def validate_item_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """→ (rows ready for rx_import_items, [{row, field, msg}])."""
    clean, errors, seen = [], [], {}
    for raw in rows:
        n = raw["_row"]
        try:
            r = ImportRow(**raw)
        except ValidationError as e:
            errors.extend({"row": n, "field": ".".join(str(x) for x in err["loc"]) or None,
                           "msg": err["msg"].removeprefix("Value error, ")} for err in e.errors())
            continue
        if r.item_code in seen:
            errors.append({"row": n, "field": "item_code", "msg": f"duplicate of row {seen[r.item_code]}"})
            continue
        seen[r.item_code] = n
        d = r.model_dump(exclude={"gst_rate_percent"})
        d.update(tax_rate_bps=int(r.gst_rate_percent) * 100, _row=n,
                 symbology=symbology_for(r.barcode) if r.barcode else None)
        clean.append(d)
    return clean, errors


def sql_row_errors(errors: list[dict]) -> list[dict]:
    """Row failures reported by an rx_* import function, as user text (never raw DB text)."""
    out = []
    for e in errors:
        known = explain(str(e.get("error") or ""))
        out.append({"row": e.get("row"), "field": None,
                    "msg": known[2] if known and known[0] < 500 else "This row could not be saved; check its values."})
    return out


async def import_items(clinic_id: str, rows: list[dict], apply: bool, actor_id: Optional[str], actor_name: str) -> dict:
    res = await sb(supabase.rpc("rx_import_items", {"p_clinic_id": clinic_id, "p_rows": rows, "p_apply": apply,
                                                    "p_actor_id": actor_id, "p_actor_name": actor_name}))
    out = dict(res.data or {})
    out["errors"] = sql_row_errors(out.get("errors") or [])
    return out

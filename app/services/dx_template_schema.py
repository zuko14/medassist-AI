"""Report template layout document, schema v1 (REPORT-TEMPLATES.md §4.1) — pure, zero-LLM.

A layout is data, never code: unknown keys are refused, colours are #RRGGBB, text is
plain (no HTML), assets live under the clinic's own storage prefix. There is no key
for the "Tested at" / "Performed at" lines: the renderer always prints them when they
apply (ADR-DX-08), so configuration cannot remove them.
"""

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HEX = r"^#[0-9A-Fa-f]{6}$"
PATIENT_FIELDS = ("name", "age_sex", "referrer", "visit_no", "mrn", "bill_no", "external_ref", "provider",
                  "collected", "received", "reported", "sample_ids")
FONTS = ("noto_sans", "helvetica", "times")
ASSET_PATH = re.compile(r"^[0-9a-f-]{36}/dx-assets/templates/[0-9a-f]{64}\.(png|jpg)$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _plain_text(self):
        for name, value in self:
            for text in (value if isinstance(value, list) else [value]):
                if isinstance(text, str) and ("<" in text or ">" in text):
                    raise ValueError(f"{name}: plain text only (no HTML)")
        return self


class Margins(_Strict):
    top: float = Field(10, ge=5, le=30)
    bottom: float = Field(12, ge=5, le=30)
    left: float = Field(12, ge=5, le=30)
    right: float = Field(12, ge=5, le=30)


class Page(_Strict):
    size: Literal["A4"] = "A4"
    margin_mm: Margins = Field(default_factory=Margins)


class Composed(_Strict):
    logo_asset: Optional[str] = None
    name: Optional[str] = Field(None, max_length=120)    # letterhead name; default: the lab's own name
    title: str = Field("DIAGNOSTIC REPORT", max_length=300)
    lines: list[str] = Field(default_factory=list, max_length=4)
    accent_hex: str = Field("#F28C28", pattern=HEX)
    band: bool = True                     # the coloured band of the "classic" look

    @field_validator("lines")
    @classmethod
    def _short(cls, v):
        if any(len(x) > 300 for x in v):
            raise ValueError("each line is at most 300 characters")
        return v


class Header(_Strict):
    mode: Literal["image", "composed"] = "composed"
    image_asset: Optional[str] = None
    height_mm: float = Field(30, ge=12, le=60)
    composed: Composed = Field(default_factory=Composed)

    @model_validator(mode="after")
    def _image_needs_asset(self):
        if self.mode == "image" and not self.image_asset:
            raise ValueError("an image header needs image_asset")
        return self


class Footer(_Strict):
    lines: list[str] = Field(default_factory=list, max_length=3)
    height_mm: float = Field(22, ge=16, le=40)
    page_numbers: bool = True


class PatientBlock(_Strict):
    fields: list[Literal[PATIENT_FIELDS]] = Field(default_factory=lambda: list(PATIENT_FIELDS))  # type: ignore[valid-type]
    show_barcode: bool = True


class Body(_Strict):
    group_by_department: bool = True
    show_department_heading: bool = True
    show_package_heading: bool = True
    show_method: bool = True
    show_previous_value: bool = True
    flag_style: Literal["bold"] = "bold"
    font_pt: float = Field(9, ge=7, le=12)
    font_family: Literal[FONTS] = "noto_sans"  # type: ignore[valid-type]
    end_marker: str = Field("END OF REPORT", max_length=60)


class Signatures(_Strict):
    placement: Literal["per_department", "end"] = "per_department"
    show_image: bool = True
    show_registration_no: bool = True


class Verification(_Strict):
    qr: bool = True
    position: Literal["footer_right", "footer_left"] = "footer_right"


class Layout(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_version: Literal[1] = Field(1, alias="schema")
    page: Page = Field(default_factory=Page)
    header: Header = Field(default_factory=Header)
    footer: Footer = Field(default_factory=Footer)
    patient_block: PatientBlock = Field(default_factory=PatientBlock)
    body: Body = Field(default_factory=Body)
    signatures: Signatures = Field(default_factory=Signatures)
    verification: Verification = Field(default_factory=Verification)
    watermark: Optional[str] = Field(None, max_length=40)
    disclaimer: str = Field("Results relate only to the sample tested. Correlate clinically.", max_length=300)


def validate_layout(raw: dict, clinic_id: Optional[str] = None) -> dict:
    """The normalised layout (defaults filled), or ValueError. With clinic_id, every asset
    path must be a content-addressed file under that clinic's template prefix."""
    layout = Layout.model_validate(raw or {})
    if clinic_id is not None:
        for path in (layout.header.image_asset, layout.header.composed.logo_asset):
            if path and (not ASSET_PATH.match(path) or not path.startswith(f"{clinic_id}/")):
                raise ValueError("assets must be uploaded to this clinic's template storage")
    return layout.model_dump(by_alias=True)


def asset_paths(layout: dict) -> list[str]:
    h = layout.get("header") or {}
    return [p for p in (h.get("image_asset"), (h.get("composed") or {}).get("logo_asset")) if p]

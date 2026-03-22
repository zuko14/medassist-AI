"""Patient Pydantic models."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class PatientBase(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20)
    name: Optional[str] = Field(None, max_length=100)
    language: Optional[str] = Field(None, pattern=r"^(en|hi|te)$")


class PatientCreate(PatientBase):
    pass


class PatientUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    language: Optional[str] = Field(None, pattern=r"^(en|hi|te)$")
    opted_in: Optional[bool] = None
    data_consent: Optional[bool] = None


class Patient(PatientBase):
    id: UUID
    opted_in: bool = False
    opted_in_at: Optional[datetime] = None
    opted_out_at: Optional[datetime] = None
    data_consent: bool = False
    data_consent_at: Optional[datetime] = None
    visit_count: int = 0
    last_seen_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True

"""Appointment Pydantic models."""

from datetime import date, datetime, time
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class AppointmentBase(BaseModel):
    patient_phone: str = Field(..., min_length=10, max_length=20)
    patient_name: Optional[str] = Field(None, max_length=100)
    department: str = Field(..., max_length=50)
    doctor_name: Optional[str] = Field(None, max_length=100)
    appointment_date: date
    appointment_time: time
    symptoms: Optional[str] = None


class AppointmentCreate(AppointmentBase):
    patient_id: Optional[UUID] = None


class AppointmentUpdate(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(confirmed|cancelled|rescheduled|completed|no_show)$")
    appointment_date: Optional[date] = None
    appointment_time: Optional[time] = None
    doctor_name: Optional[str] = None
    notes: Optional[str] = None


class Appointment(AppointmentBase):
    id: UUID
    patient_id: Optional[UUID] = None
    status: str = "confirmed"
    booking_ref: Optional[str] = None
    reminder_24h_sent: bool = False
    reminder_2h_sent: bool = False
    followup_sent: bool = False
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AppointmentResponse(BaseModel):
    success: bool
    appointment: Optional[Appointment] = None
    reason: Optional[str] = None

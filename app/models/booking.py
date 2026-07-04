"""Pydantic models for the payment-gated booking flow."""

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class BookingStatus(str, Enum):
    """Valid booking statuses — matches the DB CHECK constraint."""
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    PENDING_REVIEW = "pending_review"
    COMPLETED = "completed"
    NO_SHOW = "no_show"
    RESCHEDULED = "rescheduled"


class PaymentEventType(str, Enum):
    """Audit event types for payment_events table."""
    ORDER_CREATED = "order_created"
    WEBHOOK_RECEIVED = "webhook_received"
    SIGNATURE_VERIFIED = "signature_verified"
    SIGNATURE_FAILED = "signature_failed"
    CONFIRMED = "confirmed"
    REFUND_INITIATED = "refund_initiated"
    REFUND_COMPLETED = "refund_completed"
    REFUND_FAILED = "refund_failed"
    MISMATCH_FLAGGED = "mismatch_flagged"
    EXPIRED = "expired"
    HOLD_EXPIRED = "hold_expired"
    RECOVERY_CONFIRMED = "recovery_confirmed"
    MANUAL_CONFIRM = "manual_confirm"
    MANUAL_REJECT = "manual_reject"


class BookingCreateRequest(BaseModel):
    """Input from conversation flow to create a payment-gated booking."""
    clinic_id: str
    patient_phone: str
    patient_name: str
    department: str
    doctor_name: str
    appointment_date: str  # YYYY-MM-DD
    appointment_time: str  # HH:MM
    symptoms: Optional[str] = ""


class BookingCreateResponse(BaseModel):
    """Response after creating a booking + Razorpay order."""
    success: bool
    booking_id: Optional[str] = None
    razorpay_order_id: Optional[str] = None
    payment_link: Optional[str] = None
    amount_paise: Optional[int] = None
    hold_expires_at: Optional[str] = None
    reason: Optional[str] = None  # "slot_taken", "razorpay_error", etc.


class RefundRequest(BaseModel):
    """Admin request to initiate a refund."""
    booking_id: str = Field(..., description="UUID of the booking to refund")
    reason: Optional[str] = Field(None, description="Reason for the refund")


class RefundResponse(BaseModel):
    """Response after initiating a refund."""
    success: bool
    refund_id: Optional[str] = None
    status: Optional[str] = None
    reason: Optional[str] = None


class ManualConfirmRequest(BaseModel):
    """Admin request to manually confirm a pending_review booking."""
    booking_id: str
    admin_notes: Optional[str] = None


class BookingDetail(BaseModel):
    """Full booking detail for admin panel."""
    id: str
    clinic_id: str
    patient_phone: str
    patient_name: Optional[str] = None
    department: Optional[str] = None
    doctor_name: Optional[str] = None
    appointment_date: Optional[str] = None
    appointment_time: Optional[str] = None
    status: str
    razorpay_order_id: Optional[str] = None
    payment_id: Optional[str] = None
    amount_paise: int = 0
    hold_expires_at: Optional[str] = None
    booking_ref: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

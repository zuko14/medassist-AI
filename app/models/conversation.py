"""Conversation session Pydantic models."""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ConversationContext(BaseModel):
    for_self: Optional[bool] = None
    booking_name: Optional[str] = None
    symptoms: Optional[str] = None
    suggested_department: Optional[str] = None
    suggestion_reasoning: Optional[str] = None
    department: Optional[str] = None
    doctor_name: Optional[str] = None
    doctor: Optional[dict] = None
    appointment_date: Optional[str] = None
    appointment_time: Optional[str] = None


class ConversationBase(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20)
    state: str = "idle"
    context: dict = Field(default_factory=dict)


class ConversationCreate(ConversationBase):
    pass


class ConversationUpdate(BaseModel):
    state: Optional[str] = None
    context: Optional[dict] = None
    session_expires_at: Optional[datetime] = None
    booking_context_expires_at: Optional[datetime] = None
    last_message_at: Optional[datetime] = None
    last_processed_message_id: Optional[str] = None
    unknown_intent_count: Optional[int] = None


class Conversation(ConversationBase):
    id: UUID
    session_expires_at: Optional[datetime] = None
    booking_context_expires_at: Optional[datetime] = None
    last_message_at: Optional[datetime] = None
    last_processed_message_id: Optional[str] = None
    unknown_intent_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

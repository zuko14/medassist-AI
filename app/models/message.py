"""Incoming WhatsApp message Pydantic models."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class WhatsAppText(BaseModel):
    body: str


class WhatsAppButton(BaseModel):
    payload: str
    text: str


class WhatsAppInteractive(BaseModel):
    type: str
    button_reply: Optional[dict] = None
    list_reply: Optional[dict] = None


class WhatsAppMessage(BaseModel):
    from_: str = Field(..., alias="from")
    id: str
    timestamp: str
    type: str
    text: Optional[WhatsAppText] = None
    button: Optional[WhatsAppButton] = None
    interactive: Optional[WhatsAppInteractive] = None


class WhatsAppContact(BaseModel):
    wa_id: str
    profile: Optional[dict] = None


class WhatsAppValue(BaseModel):
    messaging_product: str
    metadata: dict
    contacts: Optional[list[WhatsAppContact]] = None
    messages: Optional[list[WhatsAppMessage]] = None
    statuses: Optional[list[dict]] = None


class WhatsAppChange(BaseModel):
    value: WhatsAppValue
    field: str


class WhatsAppEntry(BaseModel):
    id: str
    changes: list[WhatsAppChange]


class WhatsAppWebhookPayload(BaseModel):
    object: str
    entry: list[WhatsAppEntry]


class WebhookResponse(BaseModel):
    status: str
    message: Optional[str] = None

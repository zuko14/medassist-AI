"""Phase 7 WhatsApp Delivery Schemas & Domain Models."""

from enum import Enum
from pydantic import BaseModel, Field


class WhatsAppDeliveryStatus(str, Enum):
    """WhatsApp message delivery lifecycle status."""
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"


class WhatsAppTemplatePayload(BaseModel):
    """Meta WhatsApp Cloud API Interactive Media Message Payload."""

    messaging_product: str = Field(default="whatsapp", description="Messaging product indicator")
    recipient_type: str = Field(default="individual", description="Recipient type")
    to: str = Field(..., description="Recipient phone number with country code")
    type: str = Field(default="template", description="Message type")
    template_name: str = Field(default="callmedex_lab_report_summary", description="Registered Meta template name")
    language_code: str = Field(default="en", description="Language ISO code")
    header_pdf_url: str = Field(..., description="Public or signed storage URL to PDF report file")
    body_text_summary: str = Field(..., description="Patient summary text body")
    disclaimer_text: str = Field(..., description="Medical disclaimer text footer")


class WhatsAppDeliveryResult(BaseModel):
    """WhatsApp Delivery execution result."""

    message_id: str = Field(..., description="Meta WhatsApp Cloud API message tracking ID")
    status: WhatsAppDeliveryStatus = Field(..., description="Current delivery status")
    phone_number: str = Field(..., description="Target phone number")
    callback_delivered: bool = Field(..., description="True if HMAC signed callback was dispatched successfully")
    timestamp: str = Field(..., description="Timestamp of delivery execution")

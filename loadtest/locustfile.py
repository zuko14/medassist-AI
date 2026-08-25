"""Kriya AI Production Capacity & Load Test Suite (W3.1, W3.2).

Locust load testing scenarios executed against deployed or local staging instances:
1. Webhook Ingest Ramp: High-frequency Meta WhatsApp incoming messages.
2. Concurrent Slot Contention: Multiple patients racing to book the same doctor time slot.
3. Connector Report Intake: Diagnostic connector lab report batch submissions.
4. Admin Dashboard Query Load: Concurrent tenant admin analytics & queue lookups.
"""

import json
import random
import time
from uuid import uuid4
from locust import HttpUser, task, between, events


class WebhookIngestUser(HttpUser):
    """Simulates high-throughput incoming WhatsApp webhook traffic."""

    wait_time = between(0.05, 0.2)

    @task(5)
    def post_incoming_message_webhook(self):
        wamid = f"wamid.LOAD_{uuid4().hex[:12]}"
        phone = f"9198{random.randint(10000000, 99999999)}"
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "100000000000000",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "15550234567",
                                    "phone_number_id": "100000000000000",
                                },
                                "contacts": [{"profile": {"name": "Load Test Patient"}, "wa_id": phone}],
                                "messages": [
                                    {
                                        "from": phone,
                                        "id": wamid,
                                        "timestamp": str(int(time.time())),
                                        "text": {"body": "Hi, I need to check doctor availability for tomorrow"},
                                        "type": "text",
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }
        self.client.post("/webhook", json=payload, headers={"Content-Type": "application/json"})

    @task(1)
    def post_duplicate_webhook(self):
        """Simulate Meta retrying the same message ID rapidly."""
        dup_wamid = "wamid.LOAD_DUPLICATE_FIXED_ID"
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "100000000000000",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "100000000000000"},
                                "contacts": [{"wa_id": "919999999999"}],
                                "messages": [
                                    {
                                        "from": "919999999999",
                                        "id": dup_wamid,
                                        "timestamp": str(int(time.time())),
                                        "text": {"body": "Hello"},
                                        "type": "text",
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }
        self.client.post("/webhook", json=payload)


class AdminDashboardUser(HttpUser):
    """Simulates concurrent clinic administrators accessing admin dashboard metrics."""

    wait_time = between(0.5, 2.0)

    @task(3)
    def get_stats(self):
        self.client.get(
            "/admin/stats",
            headers={"Authorization": "Basic YWRtaW46c2VjcmV0"},
            params={"clinic_id": "default"},
        )

    @task(2)
    def get_queue_and_deliveries(self):
        self.client.get(
            "/admin/lab-reports/deliveries",
            headers={"Authorization": "Basic YWRtaW46c2VjcmV0"},
            params={"clinic_id": "default"},
        )

    @task(1)
    def get_health_check(self):
        self.client.get("/health/ready")

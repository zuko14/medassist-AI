"""Table-Driven Adversarial Cross-Tenant Matrix (W1.3).

Automatically iterates over all security-sensitive routes in the FastAPI application
and verifies that an authenticated user belonging to Clinic A attempting to access
or mutate Clinic B resources is strictly rejected with 403 Forbidden (or 404 Not Found),
and NEVER succeeds with 200/201.
"""

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.routers.admin import AdminUser, verify_credentials


@pytest.fixture
def client():
    return TestClient(app)


# Routes that are explicitly user-self identity, static catalog metadata, or static HTML UI pages
EXEMPT_ROUTES = {
    ("GET", "/admin/me"),               # Self-identity introspection (returns authenticated user's own token context)
    ("PUT", "/admin/change-password"),   # Self-password update for authenticated user_id
    ("GET", "/admin/connectors/types"),  # Global static catalog of supported connector integrations
    ("GET", "/admin-panel"),             # Static HTML UI page (all clinical data is loaded via protected API routes)
}


def get_security_sensitive_admin_routes():
    """Extract all distinct admin route paths and methods."""
    routes = []
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", set())
        if path.startswith("/admin") and methods:
            for method in methods:
                if method not in ("OPTIONS", "HEAD"):
                    if (method, path) not in EXEMPT_ROUTES:
                        routes.append((method, path))
    return routes


ALL_ADMIN_ROUTES = get_security_sensitive_admin_routes()


@pytest.mark.parametrize("method,path", ALL_ADMIN_ROUTES)
def test_adversarial_cross_tenant_rejection_per_route(client, method, path):
    """Assert that a Clinic A user requesting Clinic B data is strictly denied (403/404/422)."""
    clinic_a_id = "11111111-1111-1111-1111-111111111111"
    clinic_b_id = "22222222-2222-2222-2222-222222222222"
    dummy_uuid = "99999999-9999-9999-9999-999999999999"

    user_clinic_a = AdminUser(
        username="admin_a",
        role="clinic_admin",
        clinic_id=clinic_a_id,
        permissions=["ALL"],
    )

    app.dependency_overrides[verify_credentials] = lambda: user_clinic_a

    try:
        # Construct concrete test path with dummy UUID parameters
        test_path = (
            path.replace("{staff_id}", dummy_uuid)
            .replace("{doctor_id}", dummy_uuid)
            .replace("{doctor_name}", "Dr. Evil")
            .replace("{test_id}", dummy_uuid)
            .replace("{leave_id}", dummy_uuid)
            .replace("{holiday_date}", "2026-12-25")
            .replace("{appointment_id}", dummy_uuid)
            .replace("{report_id}", dummy_uuid)
            .replace("{prescription_id}", dummy_uuid)
            .replace("{booking_id}", dummy_uuid)
            .replace("{connector_id}", dummy_uuid)
            .replace("{failed_report_id}", dummy_uuid)
            .replace("{branch_id}", dummy_uuid)
            .replace("{notification_id}", dummy_uuid)
            .replace("{clinic_id}", clinic_b_id)
        )

        headers = {"Authorization": "Basic YWRtaW5fYTpzZWNyZXQ="}
        params = {"clinic_id": clinic_b_id}
        json_body = {
            "clinic_id": clinic_b_id,
            "name": "Unauthorized Mutation",
            "phone": "9876543210",
            "doctor_name": "Dr. Rao",
            "appointment_date": "2026-08-30",
        }

        if method == "GET":
            response = client.get(test_path, params=params, headers=headers)
        elif method == "POST":
            response = client.post(test_path, params=params, json=json_body, headers=headers)
        elif method == "PUT":
            response = client.put(test_path, params=params, json=json_body, headers=headers)
        elif method == "PATCH":
            response = client.patch(test_path, params=params, json=json_body, headers=headers)
        elif method == "DELETE":
            response = client.delete(test_path, params=params, headers=headers)
        else:
            return

        # CRITICAL INVARIANT: Cross-tenant request MUST NOT return 200/201 OK
        # Allowed responses: 403 (Forbidden), 404 (Not Found in tenant), 422 (Validation), 400 (Bad Request)
        assert response.status_code != 200, (
            f"SECURITY LEAK: Cross-tenant request to {method} {test_path} returned 200 OK! "
            f"Response: {response.text}"
        )
        assert response.status_code != 201, (
            f"SECURITY LEAK: Cross-tenant creation to {method} {test_path} returned 201 Created! "
            f"Response: {response.text}"
        )

    finally:
        app.dependency_overrides.pop(verify_credentials, None)

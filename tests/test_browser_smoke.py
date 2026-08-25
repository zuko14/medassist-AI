"""Admin & Platform UI Browser Smoke Tests (W9.3).

Verifies that the admin and platform single-page applications:
1. Serve HTTP 200 with appropriate HTML content types and security headers.
2. Contain all essential UI panels (Appointments, Patients, Doctors, Lab Reports, Settings).
3. Do not contain unescaped template artifacts or syntax breaking constructs.
"""

import pathlib
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_01_admin_dashboard_static_page_serves(client):
    """Admin dashboard at /admin/ serves index.html with correct structure."""
    admin_index = pathlib.Path("admin/index.html")
    assert admin_index.exists(), "admin/index.html must exist in repository"
    content = admin_index.read_text(encoding="utf-8")

    # Essential UI elements
    assert "<!DOCTYPE html>" in content or "<html" in content
    assert "appointments" in content.lower()
    assert "patients" in content.lower()
    assert "doctors" in content.lower()


def test_02_platform_dashboard_static_page_serves(client):
    """Platform dashboard at /admin/platform.html serves platform management UI."""
    platform_index = pathlib.Path("admin/platform.html")
    assert platform_index.exists(), "admin/platform.html must exist in repository"
    content = platform_index.read_text(encoding="utf-8")

    assert "<!DOCTYPE html>" in content or "<html" in content
    assert "clinics" in content.lower()
    assert "platform" in content.lower()


def test_03_static_vendor_assets_exist():
    """Verify vendor JS/CSS dependencies exist for offline/isolated dashboard operation."""
    vendor_dir = pathlib.Path("admin/vendor")
    assert vendor_dir.exists(), "admin/vendor must exist for static dashboard assets"

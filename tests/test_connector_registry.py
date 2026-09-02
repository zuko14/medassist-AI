# tests/test_connector_registry.py
"""Tests for the CONFIG_SCHEMA groundwork that lets the admin UI render a
credential form per connector type instead of hardcoding MocDoc fields."""

REQUIRED_SCHEMA_KEYS = {"key", "label", "type", "placeholder", "required"}


def test_hospital_connector_base_has_empty_default_schema():
    from connectors.base import HospitalConnector

    assert HospitalConnector.CONFIG_SCHEMA == []


def test_mocdoc_connector_config_schema_has_required_fields():
    from connectors.mocdoc.worker import MocDocConnector

    keys = [f["key"] for f in MocDocConnector.CONFIG_SCHEMA]
    assert keys == ["username", "password", "clinic_slug", "base_url", "report_routing_providers", "report_routing_phone"]
    for field in MocDocConnector.CONFIG_SCHEMA:
        assert REQUIRED_SCHEMA_KEYS <= set(field.keys())

    password_field = next(f for f in MocDocConnector.CONFIG_SCHEMA if f["key"] == "password")
    assert password_field["type"] == "password"

"""Provider-based lab report routing.

Reports booked under an insurance/TPA panel go to the diagnostic centre's TPA
desk number, never to the patient. These guard the two ways that can break:
the match rule itself, and the connector row parsing that feeds it.
"""

import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.report_routing import (
    normalize_provider,
    parse_provider_routing,
    route_recipient_for_provider,
)
from connectors.mocdoc.worker import MocDocConnector

ACCUMAX_CONFIG = {
    "username": "u",
    "password": "p",
    "report_routing_providers": (
        "VMSC MEDIBUDDY, MD INDIA TPA, MDINDIA TPA, VMSC VISIT HEALTH TPA, "
        "ASSURE TPA, HEALTH ASSURE TPA, QUANTUM CORP HEALTH MUMBAI, "
        "VMSC MD INDIA LIC TPA"
    ),
    "report_routing_phone": "9052024418",
}

DESK = "+919052024418"


@pytest.mark.parametrize(
    "provider_cell",
    [
        "VMSC MEDIBUDDY\ngmvmsc15@gmail.com",
        "VMSC MD INDIA TPA\nhydpims@mdindia.com",
        "MDINDIA TPA",
        "MD INDIA TPA",
        "VMSC VISIT HEALTH TPA",
        "HEALTH ASSURE TPA\nappointment@healthassure.i...",
        # The requested panel is "ASSURE TPA". A key only matches cells that
        # CONTAIN it, so the short form must be listed too — "HEALTH ASSURE
        # TPA" alone would miss a cell that just prints "ASSURE TPA".
        "ASSURE TPA",
        "VMSC ASSURE TPA",
        "QUANTUM CORP HEALTH MUMBAI",
        "VMSC MD INDIA LIC TPA",
    ],
)
def test_every_configured_panel_routes_to_the_desk(provider_cell):
    assert route_recipient_for_provider(ACCUMAX_CONFIG, provider_cell) == DESK


@pytest.mark.parametrize(
    "provider_cell",
    ["", None, "SELF", "CASH", "ADITYA BIRLA CAPITAL", "BAJAJ LIFE", "Healthyy SoulLife"],
)
def test_non_panel_providers_still_go_to_the_patient(provider_cell):
    assert route_recipient_for_provider(ACCUMAX_CONFIG, provider_cell) is None


def test_routing_is_off_without_both_halves():
    # A desk number with no provider list would divert every single report.
    assert route_recipient_for_provider({"report_routing_phone": "9052024418"}, "X") is None
    # A provider list with no desk number has nowhere to send.
    assert route_recipient_for_provider(
        {"report_routing_providers": "MDINDIA TPA"}, "MDINDIA TPA"
    ) is None
    # An unconfigured clinic is untouched.
    assert route_recipient_for_provider({"username": "u"}, "MDINDIA TPA") is None


def test_invalid_desk_number_disables_routing_rather_than_misdelivering():
    keys, phone = parse_provider_routing(
        {"report_routing_providers": "MDINDIA TPA", "report_routing_phone": "12"}
    )
    assert (keys, phone) == ([], None)


def test_short_provider_keys_are_ignored():
    # A 1-2 char key would substring-match nearly every provider cell.
    keys, _ = parse_provider_routing(
        {"report_routing_providers": "A, BC, MDINDIA TPA", "report_routing_phone": "9052024418"}
    )
    assert keys == ["MDINDIATPA"]


def test_normalize_provider_collapses_spacing_and_punctuation():
    assert normalize_provider("VMSC  MD-INDIA TPA.") == "VMSCMDINDIATPA"


def _table_page(rows, header_index=2):
    """Playwright page double serving one Pending Print table."""
    page = AsyncMock()
    page.goto = AsyncMock(return_value=None)
    page.wait_for_timeout = AsyncMock(return_value=None)

    async def _evaluate(script, arg=None):
        if "getElementById('pendingprint')" in script:
            return True          # tab click
        if "ths[i].innerText" in script:
            return header_index  # provider column lookup
        return False             # modals, entries dropdown, pagination

    page.evaluate = AsyncMock(side_effect=_evaluate)

    row_locators = []
    for cells_text in rows:
        cell_objs = []
        for text in cells_text:
            cell = AsyncMock()
            cell.inner_text = AsyncMock(return_value=text)
            cell_objs.append(cell)
        cells = MagicMock()
        cells.count = AsyncMock(return_value=len(cell_objs))
        cells.first = cell_objs[0]
        cells.nth = MagicMock(side_effect=lambda i, c=cell_objs: c[i])
        row = MagicMock()
        row.inner_text = AsyncMock(return_value="\n".join("\n".join(c) for c in [cells_text]))
        row.locator = MagicMock(return_value=cells)
        row_locators.append(row)

    rows_loc = MagicMock()
    rows_loc.count = AsyncMock(return_value=len(row_locators))
    rows_loc.nth = MagicMock(side_effect=lambda i: row_locators[i])

    tab = MagicMock()
    tab.click = AsyncMock(return_value=None)
    tab_loc = MagicMock()
    tab_loc.first = tab

    page.locator = MagicMock(
        side_effect=lambda sel: rows_loc if "tbody" in sel else tab_loc
    )
    return page


def _worker(config):
    return MocDocConnector(
        clinic_id="clinic-1",
        config=config,
        medassist_url="http://localhost:8000",
        integration_secret="secret",
        session_dir=tempfile.mkdtemp(),
    )


TPA_ROW = [
    "Mr.Kandula Satyanarayana\nGender: M Age: 50 years\nID: VAM-52913 Mobile: +919440545808",
    "ADITYA BIRLA CAPITAL",
    "VMSC MEDIBUDDY\ngmvmsc15@gmail.com",
    "O/P",
]
SELF_PAY_ROW = [
    "Mrs.C Varalakshmi\nGender: F Age: 60 years\nID: VAM-39927 Mobile: +918121363550",
    "",
    "SELF",
    "O/P",
]


@pytest.mark.asyncio
async def test_tpa_row_is_addressed_to_the_desk_not_the_patient():
    worker = _worker(ACCUMAX_CONFIG)
    worker._page = _table_page([TPA_ROW])

    reports = await worker.fetch_new_reports()

    assert len(reports) == 1
    meta = reports[0]
    assert meta.patient_phone == DESK
    assert meta.routed_recipient == DESK
    assert "+919440545808" not in meta.patient_phone
    assert meta.patient_name == "Mr.Kandula Satyanarayana"
    assert meta.vam_id == "VAM-52913"


@pytest.mark.asyncio
async def test_self_pay_row_is_untouched():
    worker = _worker(ACCUMAX_CONFIG)
    worker._page = _table_page([SELF_PAY_ROW])

    reports = await worker.fetch_new_reports()

    assert len(reports) == 1
    assert reports[0].patient_phone == "+918121363550"
    assert reports[0].routed_recipient is None


@pytest.mark.asyncio
async def test_clinic_without_routing_config_sends_tpa_rows_to_the_patient():
    worker = _worker({"username": "u", "password": "p"})
    worker._page = _table_page([TPA_ROW])

    reports = await worker.fetch_new_reports()

    assert reports[0].patient_phone == "+919440545808"
    assert reports[0].routed_recipient is None


@pytest.mark.asyncio
async def test_missing_provider_column_falls_back_without_crashing():
    worker = _worker(ACCUMAX_CONFIG)
    worker._page = _table_page([TPA_ROW], header_index=-1)

    reports = await worker.fetch_new_reports()

    # Fallback index is 2 — the Provider column in MocDoc's current layout.
    assert reports[0].patient_phone == DESK


def test_submit_payload_declares_the_routing_claim():
    from connectors.base import ReportMetadata

    meta = ReportMetadata(
        patient_name="Mr.X",
        patient_phone=DESK,
        report_name="CBC",
        report_type="Laboratory",
        external_report_id="VAM-1_CBC",
        provider="VMSC MEDIBUDDY",
        routed_recipient=DESK,
    )
    assert meta.provider == "VMSC MEDIBUDDY"
    assert meta.routed_recipient == DESK

    plain = ReportMetadata(
        patient_name="Mr.Y",
        patient_phone="+918121363550",
        report_name="CBC",
        report_type="Laboratory",
        external_report_id="VAM-2_CBC",
    )
    assert plain.provider is None
    assert plain.routed_recipient is None


# ═══════════════════════════════════════════════════════════════════════════
# API endpoint — the connector's "this is a TPA desk" claim is re-verified
# ═══════════════════════════════════════════════════════════════════════════


def _post_report(routed_claim, desk_is_configured):
    """POST one report to the intake endpoint; return (match_mock, upload_mock)."""
    from unittest.mock import patch
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers.integrations import router
    from app.services.patient_match import MatchResult

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    match_mock = AsyncMock(
        return_value=MatchResult(
            status="matched",
            is_safe_to_send=True,
            match_source="patients_table",
            match_confidence=1.0,
            normalized_phone="+919440545808",
            patient_name="Mr.Kandula Satyanarayana",
        )
    )
    upload_mock = AsyncMock(return_value={"id": "lr-1"})

    data = {
        "clinic_id": "clinic-A",
        "patient_phone": DESK if routed_claim else "+919440545808",
        "patient_name": "Mr.Kandula Satyanarayana",
        "report_name": "CBC",
        "external_report_id": "VAM-52913_CBC",
        "connector_type": "mocdoc",
        "provider": "VMSC MEDIBUDDY",
    }
    if routed_claim:
        data["recipient_routed"] = "true"

    with patch("app.routers.integrations.settings") as mock_settings, patch(
        "app.routers.integrations.sb", AsyncMock(return_value=MagicMock(data=[]))
    ), patch(
        "app.services.report_routing.is_routing_recipient",
        AsyncMock(return_value=desk_is_configured),
    ), patch(
        "app.services.patient_match.patient_match_service.match", match_mock
    ), patch(
        "app.routers.integrations.LabReportService"
    ) as service_cls:
        mock_settings.integration_secret = "test-secret-key"
        service_cls.return_value.upload_and_send = upload_mock
        response = client.post(
            "/internal/integrations/lab-report",
            headers={"X-Integration-Secret": "test-secret-key"},
            data=data,
            files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )

    assert response.status_code == 200, response.text
    return match_mock, upload_mock


def test_verified_routing_claim_skips_the_patient_match_gate():
    match_mock, upload_mock = _post_report(routed_claim=True, desk_is_configured=True)

    match_mock.assert_not_called()
    kwargs = upload_mock.await_args.kwargs
    assert kwargs["patient_phone"] == DESK
    assert kwargs["match_source"] == "provider_routing"
    assert kwargs["patient_name"] == "Mr.Kandula Satyanarayana"


def test_unverified_routing_claim_falls_back_to_patient_matching():
    """A connector cannot hand itself a gate bypass to an arbitrary number."""
    match_mock, upload_mock = _post_report(routed_claim=True, desk_is_configured=False)

    match_mock.assert_awaited_once()
    assert upload_mock.await_args.kwargs["match_source"] == "patients_table"


def test_ordinary_report_still_goes_through_the_gate():
    match_mock, upload_mock = _post_report(routed_claim=False, desk_is_configured=True)

    match_mock.assert_awaited_once()
    kwargs = upload_mock.await_args.kwargs
    assert kwargs["patient_phone"] == "+919440545808"
    assert kwargs["match_source"] == "patients_table"


@pytest.mark.asyncio
async def test_runner_does_not_hold_a_routed_report_for_patient_review():
    """The desk number is not in `patients`, so the walk-in gate would hold
    every TPA report forever. Routed reports must bypass it and deliver."""
    from unittest.mock import patch

    from connectors.base import ReportMetadata
    from connectors.runner import CONNECTOR_REGISTRY, run_connector
    from app.services.patient_match import MatchResult

    submitted = []

    class _FakeConnector:
        _processed_ids = set()

        def __init__(self, **kwargs):
            pass

        async def authenticate(self):
            return True

        async def fetch_new_reports(self):
            return [
                ReportMetadata(
                    patient_name="Mr.Kandula Satyanarayana",
                    patient_phone=DESK,
                    report_name="CBC",
                    report_type="Laboratory",
                    external_report_id="VAM-52913_CBC",
                    vam_id="VAM-52913",
                    provider="VMSC MEDIBUDDY",
                    routed_recipient=DESK,
                )
            ]

        async def download_report(self, meta):
            return b"%PDF-1.4 fake"

        async def submit_to_medassist(self, pdf_bytes, meta, **kwargs):
            submitted.append((meta, kwargs))
            return {"success": True}

        async def cleanup(self):
            pass

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.eq.return_value.is_.return_value.single.return_value.execute.return_value = MagicMock(
        data={
            "id": "conn-1",
            "clinic_id": "clinic-2",
            "is_enabled": True,
            "config": {
                "username": "labadmin",
                "password": "plaintext-dev-only",
                **{
                    k: ACCUMAX_CONFIG[k]
                    for k in ("report_routing_providers", "report_routing_phone")
                },
            },
        }
    )

    # If the gate ran at all it would hold this report — it fails closed here.
    gate = AsyncMock(
        return_value=MatchResult(
            status="needs_review",
            is_safe_to_send=False,
            match_source="moc_doc_only",
            match_confidence=0.0,
            review_reason="Walk-in report",
        )
    )
    store_for_review = AsyncMock(return_value="nr-1")

    with patch("connectors.runner.supabase", mock_sb), patch.dict(
        CONNECTOR_REGISTRY, {"mocdoc": _FakeConnector}
    ), patch(
        "connectors.runner.acquire_connector_lock",
        new_callable=AsyncMock,
        return_value=(True, 0),
    ), patch(
        "connectors.runner.release_connector_lock", new_callable=AsyncMock
    ), patch(
        "connectors.runner.renew_connector_lock", new_callable=AsyncMock
    ), patch(
        "connectors.runner.record_report_success", new_callable=AsyncMock
    ), patch(
        "connectors.runner.record_report_failure", new_callable=AsyncMock
    ), patch(
        "connectors.runner.send_admin_alert", new_callable=AsyncMock
    ), patch(
        "connectors.runner.notify_unverified_deliveries", new_callable=AsyncMock
    ) as notify, patch(
        "app.services.patient_match.patient_match_service.match", gate
    ), patch(
        "app.services.lab_reports.LabReportService.store_for_review", store_for_review
    ):
        result = await run_connector(clinic_id="clinic-2")

    gate.assert_not_called()
    store_for_review.assert_not_called()
    assert result["reports_needs_review"] == 0
    assert result["reports_delivered"] == 1
    assert len(submitted) == 1
    meta, kwargs = submitted[0]
    assert meta.patient_phone == DESK
    assert kwargs["match_source"] == "provider_routing"
    # A configured desk number is a verified recipient — no "unverified" alert.
    assert result["reports_delivered_unverified"] == 0
    notify.assert_not_called()

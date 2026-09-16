"""Starter treatment lists are safe to show patients, and seeding never
publishes anything or overwrites a clinic's own rows."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.clinical_firewall import validate_llm_output
from app.services.specialty_catalog import CONCERN_EXAMPLES, STARTER_TREATMENTS, seed_starter_treatments
from app.services.tenant import SPECIALTY_BY_PLAN

FORBIDDEN = ("painless", "pain-free", "guarantee", "permanent", "cure", "success rate",
             "best", "risk-free", "no side effect", "instant result", "6/6", "miracle",
             "boy", "girl", "gender", "100%")


@pytest.mark.parametrize("specialty", sorted(set(SPECIALTY_BY_PLAN.values())))
def test_every_specialty_has_a_starter_list_and_examples(specialty):
    assert len(STARTER_TREATMENTS[specialty]) >= 10
    assert CONCERN_EXAMPLES[specialty]


@pytest.mark.parametrize("specialty", sorted(STARTER_TREATMENTS))
def test_starter_rows_fit_the_database_and_whatsapp_limits(specialty):
    names = set()
    for item in STARTER_TREATMENTS[specialty]:
        name = item["name"]
        assert name.strip().lower() not in names, f"duplicate {name}"
        names.add(name.strip().lower())
        assert 1 <= len(name) <= 120
        assert 1 <= len(item["category"]) <= 60
        title = item.get("short_name") or name
        assert len(title) <= 24, f"'{title}' is too long for a WhatsApp list row"
        if item.get("short_name") is not None:
            assert len(item["short_name"]) <= 24
        assert len(item["description"]) <= 400
        assert item["description"].count("\n") == 1, f"{name}: description must be exactly 2 lines"
        assert item["concerns"] and len(item["concerns"]) <= 500
        assert item["duration_minutes"] is None or 5 <= item["duration_minutes"] <= 1440
        assert item.get("prep_instructions") is None or len(item["prep_instructions"]) <= 600


@pytest.mark.parametrize("specialty", sorted(STARTER_TREATMENTS))
def test_starter_copy_makes_no_promises_and_names_no_medicines(specialty):
    for item in STARTER_TREATMENTS[specialty]:
        text = " ".join(filter(None, [item["name"], item["description"], item.get("prep_instructions")])).lower()
        for word in FORBIDDEN:
            assert word not in text, f"{specialty}/{item['name']} contains '{word}'"
        assert validate_llm_output(item["description"], "en")[0], item["name"]
        if item.get("prep_instructions"):
            assert validate_llm_output(item["prep_instructions"], "en")[0], item["name"]


@pytest.mark.asyncio
async def test_seed_inserts_hidden_starter_rows_and_skips_existing_names():
    existing = MagicMock(data=[{"name": "  hair prp therapy "}])
    inserted = MagicMock(data=[])
    fake_supabase = MagicMock()
    with patch("app.services.specialty_catalog.supabase", fake_supabase), \
         patch("app.services.specialty_catalog.sb", AsyncMock(side_effect=[existing, inserted])):
        result = await seed_starter_treatments("clinic-1", "dermatology")

    rows = fake_supabase.table.return_value.insert.call_args.args[0]
    total = len(STARTER_TREATMENTS["dermatology"])
    assert result == {"added": total - 1, "skipped": 1}
    assert len(rows) == total - 1
    assert all(r["clinic_id"] == "clinic-1" for r in rows)
    assert all(r["is_active"] is False and r["source"] == "starter" and r["price_from_paise"] == 0 for r in rows)
    assert "Hair PRP Therapy" not in {r["name"] for r in rows}


@pytest.mark.asyncio
async def test_seed_for_unknown_specialty_touches_nothing():
    with patch("app.services.specialty_catalog.sb", AsyncMock()) as sb:
        assert await seed_starter_treatments("clinic-1", "general") == {"added": 0, "skipped": 0}
    sb.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_with_everything_present_inserts_nothing():
    names = [{"name": i["name"]} for i in STARTER_TREATMENTS["dental"]]
    with patch("app.services.specialty_catalog.sb", AsyncMock(return_value=MagicMock(data=names))) as sb:
        result = await seed_starter_treatments("clinic-1", "dental")
    assert result == {"added": 0, "skipped": len(names)}
    assert sb.await_count == 1


def test_is_uuid():
    from app.database import is_uuid

    assert is_uuid("11111111-1111-1111-1111-111111111111")
    for bad in (None, "", "more", "trt_1", 5, "11111111-1111-1111-1111"):
        assert not is_uuid(bad)


@pytest.mark.asyncio
async def test_treatment_lookups_refuse_non_uuid_ids_without_querying():
    from app import database

    with patch("app.database.sb", AsyncMock()) as sb:
        assert await database.get_treatment_by_id("clinic-1", "more") is None
        assert await database.get_treatment_doctor_ids("clinic-1", "") == set()
    sb.assert_not_awaited()


@pytest.mark.asyncio
async def test_has_active_treatments_fails_closed_to_false():
    from app import database

    with patch("app.database.sb", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await database.has_active_treatments("clinic-1") is False


@pytest.mark.asyncio
async def test_provisioning_a_specialty_clinic_seeds_its_starter_list():
    from app.routers import clinics as clinics_router

    req = clinics_router.CreateClinicRequest(
        name="Smile Dental", whatsapp_number="+919876500001", plan="dental",
        meta_phone_number_id="", meta_access_token="EAAG_test",
    )
    insert_ok = MagicMock(data=[{"id": "clinic-9", "plan": "dental"}])
    with patch("app.routers.clinics.sb", AsyncMock(side_effect=[insert_ok, MagicMock(data=[{"id": "a1"}])])), \
         patch("app.routers.clinics.seed_starter_treatments", AsyncMock(return_value={"added": 14, "skipped": 0})) as seed:
        result = await clinics_router.provision_clinic(req)
    seed.assert_awaited_once_with("clinic-9", "dental")
    assert result["starter_treatments"] == {"added": 14, "skipped": 0}


@pytest.mark.asyncio
async def test_provisioning_an_existing_plan_never_seeds():
    from app.routers import clinics as clinics_router

    req = clinics_router.CreateClinicRequest(
        name="City Poly", whatsapp_number="+919876500002", plan="polyclinic",
        meta_phone_number_id="", meta_access_token="EAAG_test",
    )
    insert_ok = MagicMock(data=[{"id": "clinic-8", "plan": "polyclinic"}])
    with patch("app.routers.clinics.sb", AsyncMock(side_effect=[insert_ok, MagicMock(data=[{"id": "a1"}])])), \
         patch("app.routers.clinics.seed_starter_treatments", AsyncMock()) as seed:
        result = await clinics_router.provision_clinic(req)
    seed.assert_not_awaited()
    assert result["starter_treatments"] is None

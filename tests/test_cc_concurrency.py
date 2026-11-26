"""Continuous Care concurrency on real Postgres (VERIFICATION §6; CG8).

Two deliveries of the same tap → one observation; two coordinators acknowledge one case → one wins (CAS 409);
overlapping evaluations of the same inputs → one stored evaluation; two doctors' taps with one idempotency key → one
action and one appointment; two radar rebuilds → one open row per source.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

from app.services import cc, cc_cases, cc_hooks, cc_radar
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
from app.utils.helpers import today_ist
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _clinic(care):
    invalidate_tenant_cache()
    return asyncio.run(get_clinic_by_id(care.clinic))


def _m():
    from app.services.whatsapp import whatsapp_service
    return SimpleNamespace(whatsapp=whatsapp_service)


def _parallel(*fns):
    with ThreadPoolExecutor(len(fns)) as ex:
        return [f.result() for f in [ex.submit(fn) for fn in fns]]


def test_double_tap_one_observation(care):  # noqa: F811
    e, pid = care.enroll()
    p = str(_q(care.conn, "INSERT INTO cc_prompts (clinic_id, enrollment_id, kind, channel, phone, status, sent_at) VALUES "
                          "(%s, %s, 'adherence', 'whatsapp_interactive', %s, 'sent', now()) RETURNING id",
               care.clinic, e["id"], care.phone(pid)))
    clinic = _clinic(care)
    tap = lambda: asyncio.run(cc_hooks.handle_tap(_m(), clinic, care.phone(pid), f"cc:adh:{p}:missed", "wamid.SAME"))  # noqa: E731
    assert _parallel(tap, tap) == [True, True]
    assert _q(care.conn, "SELECT count(*) FROM cc_observations WHERE enrollment_id = %s", e["id"]) == 1


def test_two_coordinators_one_wins(care):  # noqa: F811
    e, pid = care.enroll()
    case = asyncio.run(cc_cases.open_case(_clinic(care), patient_id=pid, enrollment_id=e["id"], reason="t2dm:hypo",
                                          tier="urgent"))
    stamp = str(_q(care.conn, "SELECT to_json(updated_at)#>>'{}' FROM cc_cases WHERE id = %s", case))
    a = care.api.U("c1", "staff", care.clinic, str(uuid.uuid4()), [], staff_role="CARE_COORDINATOR")
    b = care.api.U("c2", "staff", care.clinic, str(uuid.uuid4()), [], staff_role="CARE_COORDINATOR")
    go = lambda u: asyncio.run(_try(cc_cases.transition(care.clinic, case, "acknowledge", u)))  # noqa: E731
    res = _parallel(lambda: go(a), lambda: go(b))
    assert sorted(res) == [200, 409], res
    assert stamp


async def _try(coro):
    from fastapi import HTTPException
    try:
        await coro
        return 200
    except HTTPException as e:
        return e.status_code


def test_overlapping_evaluations_store_one_row(care):  # noqa: F811
    e, pid = care.enroll()
    _q(care.conn, "INSERT INTO cc_observations (clinic_id, enrollment_id, measure_code, value_num, observed_at, source) "
                  "VALUES (%s, %s, 'fbg', 160, now(), 'whatsapp_typed')", care.clinic, e["id"])
    clinic = _clinic(care)
    row = _q(care.conn, "SELECT row_to_json(e) FROM cc_enrollments e WHERE id = %s", e["id"])
    ev = lambda: asyncio.run(cc.evaluate_enrollment(clinic, row))["evaluation_id"]  # noqa: E731
    ids = _parallel(ev, ev, ev)
    assert len(set(ids)) == 1 and ids[0]
    assert _q(care.conn, "SELECT count(*) FROM cc_evaluations WHERE enrollment_id = %s", e["id"]) == 1
    assert _q(care.conn, "SELECT count(*) FROM cc_cases WHERE enrollment_id = %s AND status = 'open'", e["id"]) <= 1


def test_one_idempotency_key_one_appointment(care):  # noqa: F811
    e, pid = care.enroll()
    case = asyncio.run(cc_cases.open_case(_clinic(care), patient_id=pid, enrollment_id=e["id"], reason="t2dm:a1c_high",
                                          tier="watch"))
    d = today_ist() + timedelta(days=2)
    while d.weekday() > 4:
        d += timedelta(days=1)
    care.api.as_(care.u.doctor)
    key = "k-" + uuid.uuid4().hex
    post = lambda: care.api.client.post(f"/admin/cc/cases/{case}/actions", params={"clinic_id": care.clinic},  # noqa: E731
                                        json={"action_code": "book_review_visit",
                                              "params": {"date": d.isoformat(), "time": "11:00"}},
                                        headers={"Idempotency-Key": key}).status_code
    codes = _parallel(post, post)
    assert 201 in codes and set(codes) <= {201, 409}, codes
    assert _q(care.conn, "SELECT count(*) FROM cc_actions WHERE case_id = %s", case) == 1
    assert _q(care.conn, "SELECT count(*) FROM appointments WHERE patient_id = %s AND appointment_date = %s",
              pid, d.isoformat()) == 1


def test_parallel_radar_rebuilds_one_row_per_source(care):  # noqa: F811
    e, pid = care.enroll()
    _q(care.conn, "UPDATE cc_enrollments SET unanswered_streak = 4 WHERE id = %s", e["id"])
    clinic = _clinic(care)
    run = lambda: asyncio.run(cc_radar.rebuild(clinic))  # noqa: E731
    _parallel(run, run)
    assert _q(care.conn, "SELECT count(*) FROM cc_radar_rows WHERE patient_id = %s AND reason = 'not_responding' "
                         "AND closed_on IS NULL", pid) == 1

"""WhatsApp / voice / home bookings → one DX visit (W3, AUDIT A9): real HTTP, real Postgres.

Today's bookings lists confirmed, unconverted lab-test bookings; converting links
all of a patient's bookings to one visit (one line each, the home-collection fee
as a fee line); the appointment-less DX bill gets each booking's online payment
applied exactly once (full payment or deposit), retries never double it; a booking
joins one visit only.
"""

import uuid

from tests.test_dx_visits_e2e import dxw  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _booking(w, test, amount, payment_id=None, home_fee=None, day=None):
    phone = _q(w["c"], "SELECT phone FROM patients WHERE id = %s", w["patient"])
    cols = {"clinic_id": w["clinic"], "patient_id": w["patient"], "patient_phone": phone, "patient_name": "Asha",
            "department": "Lab", "appointment_date": day or _q(w["c"], "SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date"),
            "status": "confirmed", "booking_type": "lab_test", "lab_test_id": w["T"][test], "amount_paise": amount,
            "payment_id": payment_id, "payment_gateway": "razorpay" if payment_id else None,
            "booking_channel": "whatsapp", "branch_id": w["branch"]}
    if home_fee:
        cols.update({"collection_mode": "home", "home_collection_fee_paise": home_fee, "collection_slot": "07:00-09:00",
                     "collection_status": "unassigned", "collection_address": "12 MG Road", "collection_lat": 17.4,
                     "collection_lng": 78.4, "collection_contact_phone": phone})
    keys = ", ".join(cols)
    return str(_q(w["c"], f"INSERT INTO appointments ({keys}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                  *cols.values()))


def test_todays_bookings_convert_and_prepaid_applied_once(dxw):  # noqa: F811
    w, api, c = dxw, dxw["api"].as_(dxw["u"]["desk"]), dxw["clinic"]
    full = _booking(w, "lipid", 55000 + 10000, "pay_full_" + uuid.uuid4().hex[:6], home_fee=10000)  # paid incl. fee
    deposit = _booking(w, "cbc", 10000, "pay_dep_" + uuid.uuid4().hex[:6])                          # 10000 of 35000
    unpaid = _booking(w, "hba1c", None)
    tomorrow = _booking(w, "hba1c", None, day="2099-01-01")
    listed = {b["id"] for b in api.call("GET", "/admin/dx/bookings/today", c).json()["bookings"]}
    assert {full, deposit, unpaid} <= listed and tomorrow not in listed
    r = api.call("POST", "/admin/dx/bookings/convert", c, {"appointment_ids": [full, deposit, unpaid],
                                                          "idempotency_key": "convert-asha-0001"})
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["visit"]["source"] == "home_collection" and len(v["bookings"]) == 3   # one booking is a home collection
    assert [(i["kind"], i["price_source"]) for i in v["items"]] == [
        ("test", "booking"), ("fee", "fee"), ("test", "booking"), ("test", "booking")]
    listed = {b["id"] for b in api.call("GET", "/admin/dx/bookings/today", c).json()["bookings"]}
    assert not {full, deposit, unpaid} & listed                                      # converted: gone from the list
    vid = v["visit"]["id"]
    bill = api.call("POST", f"/admin/dx/visits/{vid}/bill", c, {"issue": True}).json()
    assert bill["prepaid_applied"] == 3
    inv = bill["invoices"][0]
    total = 55000 + 10000 + 35000 + 45000
    receipts = _q(w["c"], "SELECT jsonb_agg(amount_paise ORDER BY amount_paise) FROM opd_receipts WHERE invoice_id = %s "
                          "AND mode = 'prepaid_online'", inv["id"])
    assert receipts == [10000, 65000]                                                # full + deposit, the unpaid one adds 0
    again = api.call("POST", f"/admin/dx/visits/{vid}/bill", c, {"issue": True}).json()   # retry: nothing doubles
    assert again.get("prepaid_applied", 0) == 0
    assert _q(w["c"], "SELECT count(*) FROM opd_receipts WHERE invoice_id = %s", inv["id"]) == 2
    after = api.call("GET", f"/admin/dx/visits/{vid}", c).json()["invoices"][0]
    assert after["status"] == "partially_paid" and after["paid_paise"] == 75000 and after["total_paise"] == total
    # a booking joins one visit only
    r = api.call("POST", "/admin/dx/bookings/convert", c, {"appointment_ids": [full],
                                                          "idempotency_key": "convert-asha-0002"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_booking_linked"


def test_convert_refuses_mixed_patients_and_unknown_bookings(dxw):  # noqa: F811
    w, api, c = dxw, dxw["api"].as_(dxw["u"]["desk"]), dxw["clinic"]
    mine = _booking(w, "cbc", None)
    other = str(_q(w["c"], "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Ravi', %s) RETURNING id", c,
                   "+9165" + uuid.uuid4().hex[:8]))
    theirs = _booking({**w, "patient": other}, "lipid", None)
    r = api.call("POST", "/admin/dx/bookings/convert", c, {"appointment_ids": [mine, theirs],
                                                          "idempotency_key": "convert-mixed-01"})
    assert r.status_code == 422
    r = api.call("POST", "/admin/dx/bookings/convert", c, {"appointment_ids": [str(uuid.uuid4())],
                                                          "idempotency_key": "convert-ghost-01"})
    assert r.status_code == 404

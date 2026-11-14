"""Sample visit for template previews and renderer tests: the Taiyo reference report
(REPORT-TEMPLATES §1.1, HbA1c + lipid profile) as dx_pdf input dicts. Fictional patient."""

SIGNER = {"name": "Dr. K. Anitha", "qualification": "MD (Pathology)", "registration_number": "TSMC/2011/4471",
          "registration_council": "TSMC", "signature_png": None}
HBA1C_REF = "Non-diabetic: 4.0 - 6.0 %\nPre-diabetic: 6.0 - 6.5 %\nDiabetic: > 6.5 %"


def release(**over):
    base = {
        "id": "8b1f6f0e-0000-4000-8000-000000000001", "release_no": 1, "kind": "final",
        "verify_code": "Q7RZ2MNP4K", "verify_url": "https://app.kriyaai.in/public/dx/verify/Q7RZ2MNP4K",
        "created_at": "2026-09-01T01:32:00+00:00",
        "letterhead_snapshot": {
            "clinic": {"name": "Taiyo Diagnostics", "address": "Road No. 12, Banjara Hills, Hyderabad",
                       "phone": "+91 40 4000 1234"},
            "site": {"display_name": "Taiyo Central Lab", "legal_name": "Taiyo Diagnostics Pvt Ltd",
                     "address": "Banjara Hills, Hyderabad 500034", "nabl_certificate_no": "MC-4521"},
            "partners": {}},
        "routing_snapshot": {"white_label": False, "billing_mode": "patient"},
        "replaces": None, "watermark": None, "assets": {}}
    base.update(over)
    return base


def results(outsourced_lipid=False):
    hba1c = {"department": {"code": "BIO", "heading": "CLINICAL BIOCHEMISTRY", "sort_order": 1},
             "test_name": "HbA1c", "print_title": "GLYCOSYLATED HAEMOGLOBIN (HbA1c)", "package_name": None,
             "outsourced": None, "interpretation": "HbA1c reflects the average glucose of the last 2-3 months.",
             "notes": None, "attachments": [], "signer": SIGNER,
             "values": [
                 {"name": "HbA1c", "value": "7.5", "unit": "%", "flag": "high", "method": "HPLC",
                  "reference_text": HBA1C_REF, "previous_value": "7.9", "previous_at": "2026-03-12T04:00:00+00:00"},
                 {"name": "Mean Blood Glucose", "value": "168.55", "unit": "mg/dL", "flag": "not_evaluated",
                  "method": "Calculated", "reference_text": None}]}
    lipid_values = [
        ("Total Cholesterol", "134", "normal", "CHO-POD", "Desirable level < 200\nBorderline High 200-239\nHigh >= 240"),
        ("Triglycerides", "162", "high", "GPO-POD", "Normal < 150\nBorderline High 150-199\nHigh 200-499\nVery High > 500"),
        ("HDL Cholesterol", "40", "normal", "Enzymatic immunoinhibition", "40 - 60"),
        ("VLDL", "32", "normal", "Calculated", "6 - 38"),
        ("LDL Cholesterol", "61", "normal", "Calculated", "Optimal < 100\nNear optimal 100-129\nBorderline 130-159\n"
                                                           "High 160-189\nVery High >= 190"),
        ("Non-HDL Cholesterol", "94", "normal", "Calculated", "< 130"),
        ("CHOL/HDL Ratio", "3.35", "normal", "Calculated", "0 - 5.0"),
        ("LDL/HDL Ratio", "1.54", "normal", "Calculated", "0 - 3.5")]
    lipid = {"department": {"code": "BIO", "heading": "CLINICAL BIOCHEMISTRY", "sort_order": 1},
             "test_name": "Lipid Profile", "print_title": "LIPID PROFILE", "package_name": None,
             "outsourced": ({"name": "Metro Reference Lab", "legal_name": "Metro Reference Laboratories LLP",
                             "address": "Ameerpet, Hyderabad", "nabl_certificate_no": "MC-1102"}
                            if outsourced_lipid else None),
             "interpretation": None, "notes": "Fasting sample (12 h).", "attachments": [], "signer": SIGNER,
             "values": [{"name": n, "value": v, "unit": "mg/dL" if "Ratio" not in n else "Ratio", "flag": f,
                         "method": m, "reference_text": r} for n, v, f, m, r in lipid_values]}
    return [hba1c, lipid]


def visit():
    return {"accession_number": "LAB-2026-000123",
            "patient": {"name": "Mrs. Lakshmi Devi", "age_sex": "58 Y / F", "mrn": "MRN-00451"},
            "referrer": "Dr. N. Anuradha, MD DM (Neuro)", "external_ref": None, "provider": None,
            "bill_no": "INV-2026-00077", "collected_at": "2026-09-01T01:32:00+00:00",
            "received_at": "2026-09-01T02:10:00+00:00", "reported_at": "2026-09-01T06:45:00+00:00",
            "sample_ids": ["260100016583"]}

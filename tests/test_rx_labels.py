"""Dispensing labels (ADVANCED-FEATURES A4, RX-72): verbatim prescriber text, closed vocabulary only, no dose on OTC."""

from app.services.rx_pos_docs import label_text, label_vocab, render_labels_pdf, rupees_in_words

RX_LINE = {"id": "r1", "dosage": "1 tab", "frequency": "BD", "timing": "after_food", "duration_days": 5,
           "instructions": "Stop if rash appears; review in 1 week"}


def test_verbatim_prescriber_text():
    t = label_text({}, RX_LINE, "en")
    assert t["verbatim"] == "1 tab · BD · 5 days · Stop if rash appears; review in 1 week"   # never rewritten


def test_closed_vocab_translation_only():
    assert label_text({}, RX_LINE, "hi")["local"] == "दिन में 2 बार, खाना खाने के बाद"
    assert label_text({}, RX_LINE, "te")["local"] == "రోజుకు 2 సార్లు, భోజనం తర్వాత"
    assert label_vocab("1-0-1 after lunch", "any", "hi") == ""                  # outside the closed map: nothing
    assert label_vocab("bd", "bedtime", "xx") == "twice a day, at bedtime"      # unknown language → English
    assert "Stop if rash" not in label_text({}, RX_LINE, "hi")["local"]         # free text is never translated


def test_otc_label_has_no_dose():
    assert label_text({}, None, "en") == {"verbatim": "", "local": ""}
    sale = {"lines": [{"qty_units": 10, "item": {"brand_name": "Electral"}, "batch": {"batch_no": "E1",
                       "expiry_date": "2027-03-31"}, "opd_prescription_item_id": None}],
            "prescriber_snapshot": None, "walkin_name": "Walk-in", "created_at": "2026-11-17T10:00:00+05:30"}
    assert render_labels_pdf(sale, {"name": "Clinic"}, "en")[:4] == b"%PDF"


def test_amount_in_words():
    assert rupees_in_words(23500) == "Rupees Two Hundred Thirty Five Only"
    assert rupees_in_words(123456789) == "Rupees Twelve Lakh Thirty Four Thousand Five Hundred Sixty Seven and Eighty " \
                                         "Nine Paise Only"
    assert rupees_in_words(0) == "Rupees Zero Only"

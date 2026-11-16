# Kriya Pharmacy OS × Kriya Ketu, Voice and WhatsApp

> Part of the Pharmacy OS set: start at [status.md](status.md). Ketu architecture: `docs/kriya-ketu/architecture.md`;
> registry rules: `docs/kriya-ketu/action-registry-spec.md`; precedents: `docs/ipd-os/KRIYA-KETU.md`,
> `docs/diagnostic-os/KRIYA-KETU.md`. Delivered in **P9** (page registrations land with each page).

## 1. Principles
- Ketu dispatches the **same `/admin/rx/*` routes** in-process with the speaking user's session, so permissions,
  store scope, `rx_access` and audit apply unchanged. Results render through deterministic `present.*` functions.
- The L4 LLM fallback may pick only an action id and slots; it never sees stock, patient or prescription data, and
  RX reads never pass through it (ADR-RX-06). Item resolution is the deterministic `/admin/rx/items/resolve`.
- **No write actions in v1.** Anything that moves stock, money or a register is an **H** hand-off: Ketu opens the page
  prefilled and a person presses the button.
- No clinical content: Ketu never answers "what can I give instead", "what dose", or "is X safe with Y". Those
  intents return the fixed text "Kriya doesn't advise on medicines. Please check with the treating doctor." (same
  pattern as `clinical_firewall`).

## 2. Action registry (staff assistant)
`feature=_rx_on` (`rx_access(clinic) is not None`) on every action; `tests/test_assistant_registry.py` validates.

| Action | Tier | Route / page | Permission | Example triggers (en / hi-Latin / te / hi) |
|:-|:-|:-|:-|:-|
| `nav.open` → Pharmacy POS, Ward Indents, Refill Requests, Stock & Expiry, Purchase & GRN, Transfers, Stock Audit, Drug Master, Drug Registers, Pharmacy Dashboard, Pharmacy Setup | R0 | client `go(page)` | page visibility | "pharmacy billing", "open indents", "दवा स्टॉक", "ఫార్మసీ స్టాక్" |
| `rx.stock_lookup` | R0 read | `GET /admin/rx/stock/answer?q=` | RX_READ or IPD_NURSING | "Is Augmentin 625 in stock?", "Augmentin 625 hai kya", "ఆగ్మెంటిన్ 625 స్టాక్ ఉందా", "ऑगमेंटिन 625 है क्या" |
| `rx.batch_expiry` | R0 read | `GET /admin/rx/stock/{item}/batches` | RX_READ | "expiry of Dolo 650 batches", "Dolo ki expiry" |
| `rx.low_stock` | R0 read | `GET /admin/rx/stock?below_reorder=true` | RX_READ | "what is running low", "kya khatam ho raha hai" |
| `rx.near_expiry` | R0 read | `GET /admin/rx/expiry?within_days=90` | RX_READ | "near expiry medicines", "expiry wali dawa" |
| `rx.pending_indents` | R0 read | `GET /admin/rx/ipd/indents?status=raised` | RX_DISPENSE | "pending ward indents", "STAT indents" |
| `rx.my_ward_indent_status` | R0 read | `GET /admin/rx/ipd/indents?ward_id=<user's ward>` | IPD_NURSING | "has pharmacy sent bed 12 medicines", "bed 12 ki dawa aayi" |
| `rx.refill_requests` | R0 read | `GET /admin/rx/refills/requests?status=new` | RX_DISPENSE | "refill requests today" |
| `rx.today_summary` (briefing) | R0 read | `GET /admin/rx/dashboard` | RX_READ | "pharmacy sales today", morning briefing entry |
| `rx.find_bill` | R0 read | `GET /admin/rx/sales?q=` | RX_READ | "find bill PHS-2026-000431", "Lakshmi's pharmacy bill" |
| `rx.sell` | H | opens POS with Rx/patient prefilled | — | "bill Lakshmi's prescription" |
| `rx.dispense_indent` | H | opens indent | — | "dispense ward 3 indent" |
| `rx.raise_indent` | H | opens Ward Indent for the admission | — | "raise indent for bed 12" |
| `rx.receive_grn` | H | opens GRN draft for supplier | — | "inward Medplus invoice" |
| `rx.register_export` | H | opens Drug Registers with range | — | "H1 register for October" |

### 2.1 Stock answer contract (`present.rx_stock`)
Input "Is Augmentin 625 in stock?" → resolve (barcode → alias → trigram over brand + strength) →
- 1 confident match: "**Augmentin 625 Duo** (Amoxicillin + Clavulanic acid 500/125 mg tab) — OPD Counter: 6 strips +
  4 tabs (64 tabs), nearest expiry 03/2027. Central Store: 40 strips." Card buttons: *Open in Stock*, *Sell* (H).
- Out of stock: "Augmentin 625 Duo is out of stock at OPD Counter (0). **Same composition in stock:** Moxikind-CV 625
  (30 tabs), Clavam 625 (12 tabs)." — listed because they share `generic_id`; the text never says "use instead" or
  "can be given"; substitution remains a pharmacist action with reason.
- Ambiguous: up to 3 candidates as buttons ("Did you mean …?").
- Store scope: a nurse sees only their ward sub-store + "available at pharmacy: yes/no" (no quantities of other stores).

## 3. Voice receptionist (patients)
Existing intent `PHARMACY_INFORMATION` (AUDIT A48) keeps answering hours, location and phone of the pharmacy from
clinic profile. **Patients never get stock quantities by voice or WhatsApp** (stock is commercial data and invites
self-medication queries). A patient asking "is X available" gets "Please call the pharmacy at {phone} or visit the
counter." Refill requests by voice: intent `PHARMACY_REFILL` (new, deterministic keywords "refill", "repeat medicine",
"दवा दोबारा") creates `rx_refill_requests (channel='voice')` only for a caller whose phone matches a patient with an
active refill plan; otherwise routes to the pharmacy number.

## 4. WhatsApp automations
| Trigger | Template (utility) | Content rules |
|:-|:-|:-|
| Refill due (W10) | `rx_refill_due_v1` | clinic name, brand, last fill date, run-out date, "Reply REFILL / STOP". No dose, no advice |
| Refill packed | `rx_refill_ready_v1` | "Your medicines are ready for pickup at {store} until {time}. Bill: ₹{amount}" |
| Refill rejected | `rx_refill_rejected_v1` | fixed text: consult doctor + booking link |
| E-bill after sale (optional, patient opted in) | `rx_bill_v1` | bill number, amount, PDF link (signed short-lived URL, existing share-link pattern) |
Inbound keywords (`REFILL`, `STOP`, Hindi/Telugu equivalents "रिफिल", "రీఫిల్") matched deterministically on replies
whose context id equals `rx_refill_plans.last_message_id`. Free text in reply → forwarded to the clinic inbox as a
normal message; the bot does not interpret it. All sends go through the existing per-clinic credential path
(`_get_credentials`, no platform fallback — AGENTS.md §1.6) and the message ledger/quota.

## 5. Automated staff alerts
| Job (IST) | Output |
|:-|:-|
| `rx_alerts_scan` 07:00 daily | `rx_alerts` low_stock, near_expiry (≤ near_expiry_days), expired_on_hand; Ketu morning briefing line "3 items below reorder, ₹12,400 expiring in 30 days, 1 expired batch on shelf" |
| STAT indent raised | in-app notification to RX_DISPENSE users of the target store (existing `admin_notifications`) |
| `rx_ledger_check` 02:30 | ledger_mismatch alert (P1) |
| `rx_register_verify` 02:45 | register_chain_break alert (P1) |
All jobs: CAS lock, iterate only `clinics WHERE rx_state <> 'OFF'`, per-clinic try/except so one clinic's error never
stops others; masked phones in logs.

## 6. Tests
`tests/test_rx_ketu.py`: stock answer (match, out-of-stock with same-generic list, ambiguous, nurse scope), clinical
question refusal text, no write action registered, every action has `feature=_rx_on`; RT-14 page table includes the
eleven RX pages; voice `PHARMACY_REFILL` only for patients with active plans; keyword reply matching ignores messages
without matching context id.

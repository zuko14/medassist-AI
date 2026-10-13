# Kriya Ketu — UX and Visual Design Specification

Parent: [spec.md](spec.md) · ADR: [ADR-005](decisions/ADR-005-widget-and-frontend-integration.md) ·
Requirements: KK-FR-001..009, 034, KK-NFR-A11Y-*, KK-NFR-MOB-01

Everything here reuses the **existing** "Clinical Depth" system in `admin/index.html`: tokens
(`:20-107`), glass-dialog (`:1785`), focus ring (`:382-389`), the no-blur fallback (`:1242`), the
reduced-motion blocks (`:1143`, `:1158`), IBM Plex Sans, `.btn` / `.btn-accent`, and the `#kriya-mark`
sprite. **No new library, font, CSS framework or animation dependency.**

---

## 1. Brand mark

**Audit finding:** there is no Kriya Ketu asset in the repo. The only mark is `#kriya-mark`
(`admin/index.html:1852`): a hex outline, a green stem and a gradient K-arm with a mint diamond at the
joint.

**v2 (2026-10-14): three drawn concepts replace the v1 sketch.** Sources (64×64, brand gradients only):

| Concept | File | Idea | Assessment |
|---|---|---|---|
| **A · Dhūmaketu — APPROVED 2026-10-14** | [assets/ketu-mark-comet.svg](assets/ketu-mark-comet.svg) | The K's upper arm is a rising trail leaving the stem and ending in a glowing four-point star: *Kriya* (action) launching, *Ketu* (comet / guiding light) leading | Most memorable at launcher size; the K stays legible; the star carries the identity |
| B · Dhvaja | [assets/ketu-mark-banner.svg](assets/ketu-mark-banner.svg) | *Ketu* = banner: a flagpole stem with a swallow-tailed pennant forming the K, and a star cut out of it | Bold, but can read as a generic flag/report icon in a UI |
| C · Lunar node | [assets/ketu-mark-node.svg](assets/ketu-mark-node.svg) | Ketu is the lunar node: two orbital arcs form the K's arms, with the star on the node | The most refined; a strong alternative |

Visual review page (dark/light, 128→16 px ramp, launcher and widget-header mock-ups): the published
artifact "Kriya Ketu Mark". The concepts were rendered and inspected; concept A was revised once (the
trail now attaches to the stem).

Rules: ships as an inline `<symbol id="ketu-mark">` in the existing sprite next to `#kriya-mark`
(`admin/index.html:1852`), gradient ids prefixed `ketu*` to avoid collisions; no hex outline (it stays a
sub-brand of the Kriya logo); the star glint animates only under `prefers-reduced-motion: no-preference`;
it does not resemble the ChatGPT knot, the Gemini sparkle, the Claude burst or the Meta ring (the star
lives *inside* a K). **DP-08 decided: concept A.** B and C stay in `assets/` as rejected alternatives.

---

## 2. Tokens (new names alias existing tokens; nothing global is overridden)

Defined on `#ketuPanel, #ketuLauncher` (scoped), not on `:root`:

| Token | Dark (`:root`) | Light (`[data-theme="light"]`) | Source |
|---|---|---|---|
| `--ketu-surface` | `linear-gradient(160deg, rgba(36,58,82,.82) 0%, rgba(14,25,38,.90) 60%, rgba(10,19,30,.94) 100%)` | `linear-gradient(160deg, rgba(255,255,255,.90), rgba(236,244,245,.94))` | `.glass-dialog` (alpha raised for text contrast) |
| `--ketu-surface-solid` | `var(--surface-solid)` `#0E1926` | `#FFFFFF` | no-blur fallback |
| `--ketu-blur` | `blur(24px) saturate(160%)` | same | between `--blur` 20 and dialog 28 |
| `--ketu-border` | `var(--border2)` | `var(--border2)` | |
| `--ketu-rim` | `inset 0 1px 0 var(--rim)` | same | |
| `--ketu-shadow` | `0 24px 64px rgba(0,0,0,.55), 0 0 40px rgba(61,139,253,.10)` | `0 18px 48px rgba(7,24,32,.18)` | `--shadow-lg` + accent glow |
| `--ketu-radius` | `var(--radius-xl)` 22px | | |
| `--ketu-radius-card` | `var(--radius)` 14px | | |
| `--ketu-gap` | 12px; card padding 14px; panel padding 16px | | 4-px grid used in the panel |
| `--ketu-user-bubble` | `var(--accent-soft2)` + `var(--accent-border)` | same tokens | |
| `--ketu-ok` / `--ketu-warn` / `--ketu-err` | `--green` / `--amber` / `--red` with their `-bg` | | |
| `--ketu-rec` | `#F2555F` (`--red`) | | recording dot |
| Motion | open 250ms (`--motion-base`) `var(--ease-out)`; hover 150ms (`--motion-fast`) | | |
| Type | IBM Plex Sans inherited; panel 0.88rem/1.5; card title 0.92rem 600; meta 0.76rem `--text2`; numbers `font-variant-numeric: tabular-nums` | | |
| Focus | inherits `:focus-visible` 2px `--accent`, offset 2–3px | | |

**Contrast rule:** body text sits on the 0.82+ alpha surface, or on cards (`--surface2`), never directly on
blur over page content. Measured target ≥ 4.5:1 for `--text` and `--text2` in both themes. If a
measurement fails, raise the alpha rather than change the colours.

**Fallbacks:** `@supports not (backdrop-filter…)` → `--ketu-surface: var(--ketu-surface-solid)`.
`prefers-reduced-transparency: reduce` → solid as well. `prefers-reduced-motion: reduce` → no
transform or scale, opacity fade only, and a static recording dot.

---

## 3. Launcher

- `<button id="ketuLauncher" aria-label="Open Kriya Ketu assistant" aria-expanded="false"
  aria-controls="ketuPanel">`: a 52×52 circle, `--ketu-surface`, rim + shadow, with `#ketu-mark` at 26 px.
- Position: `position: fixed; right: 20px; bottom: 20px; z-index: 540`. When the super-admin **scope
  banner** is present (fixed bottom-right, z 900, `renderClinicScopeBanner()`), the launcher moves up by
  the banner's height + 8 px (read once with `getBoundingClientRect` and on resize).
- Mobile (≤ 640 px): 48×48 at `right: 16px; bottom: calc(16px + env(safe-area-inset-bottom))`.
- Keyboard shortcut: **Ctrl/⌘ + K** toggles the panel (ignored when focus is in another input or textarea
  unless the panel is already open). It is shown in the launcher tooltip.

| State | Visual |
|---|---|
| Normal | Glass circle, mark at full colour |
| Hover | `translateY(-1px)`, the border becomes `--accent-border`, and the tooltip "Kriya Ketu · Ctrl K" appears after 400 ms |
| Focus-visible | Existing 2px accent outline, offset 3px |
| Open (`aria-expanded=true`) | The mark rotates 0→45° once (200 ms; none under reduced motion) and the background becomes `--accent-soft2` |
| Busy (a run executing while the panel is closed) | A 2px conic ring spins around the circle (1.2 s linear; under reduced motion it is static and half-filled) |
| Attention (only when a run left the panel in `NEEDS_CONFIRMATION` or `UNKNOWN_OUTCOME` and the panel is closed) | An 8px amber dot at the top-right, with `aria-label` "Kriya Ketu — 1 action needs your attention". **No decorative badges.** |
| Disabled (feature off) | Not rendered at all |

---

## 4. Panel

### 4.1 Geometry
| Viewport | Size and position |
|---|---|
| ≥ 1024 px | `width: 420px; height: min(640px, calc(100vh - 120px))`, anchored `right: 20px; bottom: 84px` (above the launcher) |
| 641–1023 px | `width: min(420px, calc(100vw - 32px))`, same anchor |
| ≤ 640 px | Full-screen sheet: `inset: 0`, `height: 100dvh`, top radius 0. The composer sticks to `visualViewport` (it moves above the on-screen keyboard) |

`z-index: 560`, below confirm (600), toast (700), scope banner (900), notification drawer (999) and the
session-expired overlay (9999). When the notification drawer opens, the panel stays open (they are
different corners). When `confirmOverlay` opens (from a page action), the panel gets
`inert` + 0.5 opacity until it closes.

### 4.2 Regions
```
┌────────────────────────────────────────────┐  header 52px
│ ✦ Kriya Ketu         Clinic: Aura · Branch ▾│  scope pill (read-only; super_admin sees clinic name)
│                      [lang: Auto ▾]  [—] [✕]│  minimise, close
├────────────────────────────────────────────┤
│  conversation (scroll, aria-live=polite)    │  flex 1
│                                             │
├────────────────────────────────────────────┤
│  suggestion chips (only when empty / idle)  │
├────────────────────────────────────────────┤  composer
│ ┌────────────────────────────────────┐ 🎙 ➤ │  textarea auto-grow 1–6 lines
│ └────────────────────────────────────┘      │
│ Enter to send · Shift+Enter new line  0/1000│  meta row
└────────────────────────────────────────────┘
```

### 4.3 Message and card types (each maps to a `RunOut.cards[].type`)
| Card | Look | Used for |
|---|---|---|
| `user` | Right-aligned bubble `--ketu-user-bubble`; a "🎙 edited" chip if voice + edited | The input |
| `text` | Left, no bubble, `--text` | Short assistant reply (≤ 2 sentences) |
| `table` | Card with ≤ 8 rows, "Show all in <page>" link → `go(page)` | R0 results |
| `clarify` | Card, amber left rule, real form controls (doctor `<select>` from real doctors, date input, radio) + "Continue" | NEEDS_CLARIFICATION |
| `preview` | Card, accent left rule; title = action summary; "What will happen" bullets; affected table; warnings (amber); buttons **Confirm** (`.btn-accent`) / **Cancel** | NEEDS_CONFIRMATION (R1/R2) |
| `preview-typed` | As preview, plus "Type **12** to confirm" input; Confirm disabled until it matches | R3 |
| `steps` | Vertical list; each step has an icon and a state word (Waiting · Running · Done · Failed · Needs confirmation · Needs you) | Playbooks / execution |
| `result-ok` | Green tick + "Verified" chip + evidence line ("Leave saved for 14 Oct, read back from Doctor Leaves") + links | COMPLETED_VERIFIED |
| `result-unknown` | Amber "Couldn't confirm the result" + "Check again" button + "Open <page>" | UNKNOWN_OUTCOME / NEEDS_HUMAN |
| `result-partial` | Steps card with the failed step red; text "Steps 1–2 were completed and are still in effect." | PARTIALLY_COMPLETED |
| `error` | Red rule, fixed copy by `error_code`, recovery button | FAILED / REJECTED |
| `help` | Muted card + link | help.howto / unsupported |

**Never** render an unexecuted plan with a tick. Ticks appear only on `COMPLETED_VERIFIED` steps.

### 4.4 Suggestions (empty state)
These come from the user's permitted actions (server-provided, at most 4). Examples: "Today's
appointments", "Report delivery failures today", "Doctors on leave this week", "Open OPD queue". There is
no static list that offers what the user cannot do.

---

## 5. Wireframes (each maps to a state in [spec.md](spec.md) §5)

**W1 Launcher closed** (`widget CLOSED`)
```
                                                    ╭───╮
                                    page content     │ ✦ │  ← 52px glass circle, bottom-right
                                                    ╰───╯
```

**W2 Opened, empty** (`OPEN_IDLE`, no runs)
```
│ ✦ Kriya Ketu                    Aura Hospital  ✕ │
│                                                  │
│      ✦  How can I help at Aura Hospital?          │
│      I can look things up and do tasks you        │
│      have access to.                              │
│  [Today's appointments] [Report failures today]   │
│  [Doctors on leave this week] [Open OPD queue]    │
│ ┌──────────────────────────────────────────┐ 🎙 ➤ │
│ │ Ask or tell Kriya Ketu…                   │      │
```

**W3 Text conversation** (R0 completed)
```
│                     ┌──────────────────────────┐ │
│                     │ show today's appointments│ │
│                     └──────────────────────────┘ │
│ ✦ 14 appointments today · 3 checked in            │
│ ┌──────────────────────────────────────────────┐ │
│ │ 09:30  R. Kumar      Dr. Srinivas  confirmed │ │
│ │ 09:45  S. Devi       Dr. Anitha    checked in│ │
│ │ …                                            │ │
│ │ Show all 14 in Appointments →                │ │
│ └──────────────────────────────────────────────┘ │
```

**W4 Recording** (`voice RECORDING`)
```
│ ┌──────────────────────────────────────────┐     │
│ │ ● Recording…  0:07 / 0:30   ▁▃▅▂▆▃▁       │ ■ ✕ │  ■ stop  ✕ cancel
│ └──────────────────────────────────────────┘     │
│ Speak your request. It won't be sent until you    │
│ press Send.                                       │
```
Level meter = `AnalyserNode` RMS, 7 bars, 10 fps (hidden under reduced motion; the time counter stays).

**W5 Transcribing → transcript ready** (`TRANSCRIBING` → `TRANSCRIPT_READY`)
```
│ ┌──────────────────────────────────────────┐ 🎙 ➤ │
│ │ ◌ Turning speech into text…               │     │   (composer disabled, Esc cancels)
│ └──────────────────────────────────────────┘     │
                    ↓
│ 🎙 From voice · Telugu · check before sending   ↺ │   ↺ = re-record
│ ┌──────────────────────────────────────────┐ 🎙 ➤ │
│ │ Mark Dr. Srinivas on leave tomorrow|      │     │   cursor at end, focused
```

**W6 Editing the transcript** (`USER_EDITING`): same as W5. The chip changes to "From voice · edited".
The original is kept (KK-FR-007).

**W7 Processing** (`OPEN_BUSY`, run RECEIVED/RESOLVED)
```
│                  ┌──────────────────────────────┐ │
│                  │ Mark Dr. Srinivas on leave   │ │
│                  │ tomorrow                     │ │
│                  └──────────────────────────────┘ │
│ ✦ ◌ Understanding…                                │  → after 1.5s on LLM route: "◌ Thinking…"
```

**W8 Action-plan preview** (`steps` + `preview`, PB-01)
```
│ ✦ Plan for Dr. Srinivas Rao · Thu 15 Oct           │
│  ✓ Found doctor        Dr. Srinivas Rao (Cardio)   │
│  ✓ Read schedule       no leave on 15 Oct           │
│  ✓ Found 6 confirmed appointments                   │
│  ○ Apply leave         needs your confirmation      │
│ ┌ Mark leave · full day · Thu 15 Oct ─────────────┐ │
│ │ What will happen                                │ │
│ │ • Patients can't book Dr. Srinivas Rao that day │ │
│ │ • 6 bookings below will be cancelled by the     │ │
│ │   08:00 job tomorrow; paid ones refunded;       │ │
│ │   patients get the doctor-leave WhatsApp        │ │
│ │ 10:00 R. Kumar  ₹500 paid  …  (6 rows)          │ │
│ │                    [ Cancel ]  [ Confirm leave ]│ │
│ │ Expires in 4:59                                  │ │
│ └──────────────────────────────────────────────────┘ │
```

**W9 Confirmation required with warning** (D-01 window, R3 offer)
```
│ ┌ ⚠ Automatic cancellation won't run in time ─────┐ │
│ │ It's after 08:00, so tomorrow's 6 bookings will   │ │
│ │ NOT be cancelled or notified automatically.       │ │
│ │ [ Also cancel & notify these 6 now… ]             │ │ → opens preview-typed (R3)
│ └──────────────────────────────────────────────────┘ │
│ ┌ Cancel 6 appointments · notify · refund paid ───┐ │
│ │ Type 6 to confirm  [   ]      [Cancel] [Confirm]│ │
```

**W10 Running multi-step**
```
│  ✓ Apply leave          Done                       │
│  ◌ Verify leave         Running…                   │
│  ○ Report               Waiting                    │
```

**W11 Verified success**
```
│ ✓ Leave saved · Verified                           │
│   Read back from Doctor Leaves: 15 Oct, full day.  │
│   [Open Doctor Leaves]  [Show affected bookings]   │
```

**W12 Partial failure**
```
│  ✓ Apply leave            Done (still in effect)   │
│  ✓ Cancel 4 of 6          Done                     │
│  ✕ Cancel 2 of 6          Failed: refund failed    │
│ Steps marked Done are saved. 2 bookings need       │
│ attention. [Open Payments] [Open Appointments]     │
```

**W13 Authorization denial**
```
│ ⛔ You don't have permission to create doctor      │
│    leaves. Ask your clinic admin for "Create        │
│    doctor leaves".                                  │
```

**W14 OpenRouter unavailable / over budget**
```
│ ✦ I couldn't match that. Try:                      │
│   [Mark a doctor on leave] [Find appointments]     │
│   [Report delivery failures]                       │
│   (Smart matching is unavailable right now.)       │  ← admins only see "AI budget reached"
```

**W15 Speech unavailable**
```
│ 🎙̸ Voice service unavailable — type your request.  │  mic button disabled with tooltip
```

**W16 Mobile full-height** (375×812)
```
┌─────────────────────────────┐
│ ✦ Kriya Ketu      Aura   ✕ │  header sticky
│                             │
│   conversation              │
│                             │
├─────────────────────────────┤
│ [textarea……………] 🎙 ➤       │  sticks above the keyboard (visualViewport)
└─────────────────────────────┘
```

**W17 Keyboard-only**: Ctrl+K opens and focuses the textarea → type → Enter → Tab moves through card
controls in DOM order → Confirm with Enter/Space → Esc closes (or cancels a recording or upload first) →
focus returns to the launcher.

---

## 6. Interaction rules

| Rule | Spec |
|---|---|
| Send | Enter (not `isComposing`), or the ➤ button. Disabled when empty, over 1000 chars, or while a request is in flight. `client_request_id` is generated **per send click** and stored with the draft; a retry reuses it (KK-FR-003). |
| Draft | Saved to `sessionStorage['ketu_draft']` on input (debounced 300 ms). Cleared only on a 2xx with `run_id` (KK-FR-005). |
| Cancel in flight | Before the response, Esc aborts the fetch (`AbortController`). The server run may still exist; the widget re-syncs via `GET runs?active=1`. For R1+ runs nothing executes without confirm, and R0 is harmless. |
| Confirm | The button disables on click and shows a spinner. A second click is ignored. The server is idempotent anyway (CAS). |
| Expiry | Countdown shown on preview cards. At 0: buttons disable and "Expired — ask again" appears. |
| Close | Esc or ✕. A busy run continues; the launcher shows the busy ring. Reopen restores. |
| Scroll | Auto-scroll to newest only if the user is within 80 px of the bottom; otherwise a "New result ↓" pill. |
| Focus | Open → textarea. New card with controls → announce via live region, without stealing focus. After Confirm → focus the result card heading (`tabindex=-1`). |
| Links | "Open <page>" calls `go(page)` and **minimises** the panel on mobile, keeping it on desktop. |

## 7. Accessibility

- Panel: `role="dialog" aria-modal="false" aria-labelledby="ketuTitle"`. It is non-modal, so the page stays
  usable. The conversation is `role="log" aria-live="polite" aria-relevant="additions"`.
- Recording state is announced ("Recording started", "Recording stopped, transcribing", "Transcript
  ready, edit and press Send").
- All icons have text labels (`aria-label`). State is never shown by colour alone (each state has a word
  and an icon).
- Hit targets ≥ 44×44 on mobile; ≥ 32×32 on desktop.
- Typed-confirmation input: `aria-describedby` points to the "Type 6 to confirm" text.
- Tested with axe-core (Playwright) and a manual NVDA + Chrome pass (KK-TEST-UI-*).

## 8. Theme

Both themes come from the existing `data-theme` attribute (the restore logic at `admin/index.html:6159`).
Light theme: the surface becomes white glass at 0.90–0.94 alpha, the text uses the existing light tokens,
and the shadow is softer. The OPD pages' light-theme overrides do not touch the panel, because it lives
outside `#pg-*` sections.

## 9. Component hierarchy (inline functions in `admin/index.html`, no framework)

See [diagrams/frontend-components.mmd](diagrams/frontend-components.mmd).
```
ketu = {                      // single namespace object, like window.opd
  state, runs[], voice{},     // client state
  init(me),                   // called after /admin/me resolves; renders launcher or nothing
  open(), close(), toggle(),
  send(text), confirm(run), cancel(run), sync(),
  renderRun(run) → card renderers: cardTable, cardClarify, cardPreview, cardSteps, cardResult, cardError, cardHelp
  mic: start(), stop(), cancel(), upload(blob) → fills composer
}
```
All dynamic text goes through the existing `esc()`. No `innerHTML` with server strings that aren't escaped.

---

## 10. v2 card types and wireframes (full-system scope)

New `RunOut.cards[].type` values and how they render:

| Card | Purpose | Key rules |
|---|---|---|
| `plan` | Whole-playbook preview (L3): numbered steps, **every write listed with counts**, skip list with reasons, one Confirm (typed count when > 10) | A step that later diverges reopens this card for the remaining steps only |
| `briefing` | Morning / end-of-day: sections with one number, one sentence and one action chip each | Sections the role can't read are absent, not empty |
| `consent` | Two required yes/no questions asked to the patient (data storage, WhatsApp) | No default; Continue disabled until both are answered |
| `diff` | Old → new values (prices, schedule, settings); rows sortable; totals | Rounding rule printed above the table |
| `handoff` | "This needs your click on the <page> page", Open & fill button, list of the fields that will be pre-filled | Passwords never listed or filled; the assistant shows nothing it would submit |
| `attach` | One file chip (name, size) for catalogue import, upload progress | Only on PB-09; any other action shows "attachments aren't used for this" |
| `funnel` | Lead conversion: queued → dialled → connected → booked | Numbers link to the Voice / Leads / Appointments pages |

**W18 Leads → calls plan (PB-03)**
```
│ ✦ Call 12 hot dermatology leads                     │
│  1 ✓ Read leads           15 found (last 7 days)    │
│  2 ✓ Checked who can be called                      │
│      12 will be called · 3 skipped                  │
│      ├ 1 opted out           ├ 1 called on Mon      │
│      └ 1 already queued                             │
│  3 ○ Queue calls          10:00–19:00 · cap 30/day  │
│      ≈ 12 today, first at 10:00                     │
│ Type 12 to confirm [  ]          [Cancel] [Confirm] │
```

**W19 Morning briefing (PB-12)**
```
│ ✦ Good morning · Tue 14 Oct                         │
│  Appointments   42 today · Dr Rao 14, Dr Anitha 11  [Open]
│  Leaves         Dr Srinivas (full day)              [Open]
│  Hot leads      6 since yesterday                   [Call them]
│  Reports        2 failed deliveries                 [Resend]
│  Voice          18 calls yesterday · 5 bookings     [Details]
```

**W20 Register + token with consent (PB-06)**
```
│ ✦ Register Lakshmi · 98765·43210 · 42 · F           │
│  Possible match: "Lakshmi Devi", same phone          │
│   ( ) Same person — use existing  ( ) New person     │
│  Ask the patient:                                    │
│   Store their details?        ( ) Yes  ( ) No        │
│   WhatsApp updates?           ( ) Yes  ( ) No        │
│  Then: token for Dr Anitha                           │
│                          [Cancel]  [Register + token]│
```

**W21 Bulk price diff (PB-07)**
```
│ ✦ Biochemistry +10 %, rounded to ₹10 · 23 tests     │
│  Test              Old     New                      │
│  Lipid profile     ₹600 →  ₹660                     │
│  LFT               ₹550 →  ₹610                     │
│  …  (23 rows)      Total change +₹1,240             │
│ Type 23 to confirm [  ]          [Cancel] [Confirm] │
```

**W22 Hand-off (staff account)**
```
│ ✦ New staff accounts need your click                │
│   I'll open Staff & Roles and fill: name, role       │
│   (Receptionist), branch (Main). You set the         │
│   password and press Create.                         │
│                                   [Open & fill]      │
```

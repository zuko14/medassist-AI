# ADR-005 — Inline glass widget in admin/index.html using existing tokens

Status: **Proposed** · Date: 2026-10-13

## Context
The clinic panel is a single inlined HTML/CSS/JS file (`admin/index.html`, 19,678 lines, no build step).
CSP is `script-src 'self' 'unsafe-inline'` with `connect-src 'self'`, and there are no CDNs. A complete
"Clinical Depth" glass system exists: tokens, `.glass-dialog`, focus rings, the no-blur fallback,
reduced-motion blocks, light theme, and the `#kriya-mark` sprite. `admin/admin.js` is dead.

## Options
1. Inline the widget in `admin/index.html` (CSS block + sprite symbol + `ketu` namespace script).
2. A separate static file (`admin/ketu.js` / `.css`) served via a new static route.
3. A web component or small framework (Lit/Preact), vendored.
4. A third-party chat widget.

## Decision
Option 1.

## Reasons
- It matches the house rule ("Frontend is Inlined") and the existing pattern for big features (OPD OS,
  home collection and voice pages are all inline).
- It reuses `api*`, `withScope`, `esc`, `go`, `toast` and the 401 handling directly.
- Option 2 needs a new static route and cache-busting, and splits the source of truth.
- Option 3 adds a dependency and a second rendering model. Option 4 conflicts with CSP, privacy and
  branding.

## Consequences
- The file grows ~30 KB. Mitigated by lazy DOM creation on first open.
- One agent edits the file at a time (the implementation plan serializes P2/P3).
- `ketu.init` is wrapped in try/catch so a widget failure cannot block the panel.

## Risks
Global keydown (Ctrl+K) conflicts; z-index collisions with drawers; mobile keyboard overlap. All are in
the UI test matrix.

## Verification
UI-01..18, existing panel tests, a Lighthouse before/after (KK-NFR-PERF-02).

## Revisit when
The panel is split into modules or gets a build step. Then move the widget to its own module unchanged.

# KRIYA AI — DESIGN SYSTEM SPECIFICATION

**v1.0.0 · October 2026 · Confidential**

---

## Table of Contents

1. [Brand Identity](#1-brand-identity)
2. [Colour System](#2-colour-system)
3. [Typography](#3-typography)
4. [Spatial System](#4-spatial-system)
5. [Surface & Glass Model](#5-surface--glass-model)
6. [Background & Aura System](#6-background--aura-system)
7. [Component Library](#7-component-library)
8. [Iconography](#8-iconography)
9. [Motion & Animation](#9-motion--animation)
10. [Responsive Framework](#10-responsive-framework)
11. [Accessibility](#11-accessibility)
12. [Application to Products](#12-application-to-products)

---

# 1. Brand Identity

## 1.1 The Kriya Mark

The logo is a **geometric hexagonal shield** containing an abstract "K" letterform rendered as two strokes:

| Element | Description | Gradient |
|:--|:--|:--|
| **Outer Hexagon** | Thin-stroke hexagonal boundary, 40% opacity | `#43E2A6` → `#3D8BFD` (top-left to bottom-right) |
| **Vertical Stem** | Bold vertical stroke (left pillar of the "K") | `#43E2A6` → `#0C9668` (top to bottom) |
| **Diagonal Arm** | Bold chevron stroke (right arm of the "K") | `#3D8BFD` → `#2FA8D8` → `#43E2A6` (bottom to top) |
| **Central Diamond** | Small filled diamond at the stroke intersection | Solid `#9FF0D2` |

```
SVG Source (canonical 32x32 viewBox):
──────────────────────────────────────
Hexagon:   M16 2.2 27.9 9v13.6L16 29.4 4.1 22.6V9z
Stem:      M11.2 7.6v16.8  (stroke-width: 3.6, round caps)
Arm:       M22.9 7.6 14.2 16l8.7 8.4  (stroke-width: 3.6, round caps & joins)
Diamond:   M14.2 13.3 16.9 16l-2.7 2.7L11.5 16z
```

### Size Specifications

| Context | Size | Notes |
|:--|:--|:--|
| Login screen mark | `62x62px` container, `36x36px` SVG | Placed inside a glass tile with rim highlight |
| Sidebar header | `24x24px` | Inline with "Kriya AI" wordmark |
| Owner panel header bar | `26x26px` | Inline with brand wordmark |
| Favicon | `32x32px` | Direct SVG at native viewBox size |

### Usage Rules

1. **Never place the gradient mark on a gradient background.** The mark sits on a dark glass surface (`--surface2`) with a `--rim` border — this provides the necessary contrast.
2. **The mark is always accompanied by the wordmark** "Kriya AI" set in IBM Plex Sans at weight 600–700.
3. **Minimum clear space** around the mark equals the width of the central diamond (~4px at 32px scale).

---

## 1.2 Brand Gradient

The signature gradient that defines the entire product language:

```css
--brand-grad: linear-gradient(135deg, #10B981 0%, #2FA8D8 55%, #3D8BFD 100%);
```

| Stop | Hex | Role |
|:--|:--|:--|
| 0% | `#10B981` | **Emerald** — clinical trust, vitality |
| 55% | `#2FA8D8` | **Teal** — transition point, depth |
| 100% | `#3D8BFD` | **Sapphire** — intelligence, precision |

### Where the gradient appears (and nowhere else):

- Logo mark gradients
- Primary CTA button (`.btn-accent`)
- Active navigation rail indicator (3px bar)
- Stat card top-edge hairline (1px, 50% opacity)
- Usage progress bar fill
- **Never on backgrounds, text fills, or decorative blobs**

---

## 1.3 Design Philosophy: "Clinical Depth"

The design language is named **"Clinical Depth"** — a deliberate intersection of medical precision and modern glassmorphism.

**Core principles:**

| Principle | Implementation |
|:--|:--|
| **Translucent surfaces** | All containers use `rgba()` backgrounds with `backdrop-filter: blur(20px)` |
| **Mint-rimmed glass** | A `#C7EAE1` at low alpha creates the signature clinical edge |
| **Restrained colour** | Colour is reserved for *meaning*, never decoration. Numbers are neutral white; only exception states (failures, warnings) get colour. |
| **Navy-as-void** | The `#040A11` background is not black — it is the same deep navy as the logo's hexagonal field |
| **One loud moment** | The emerald-to-sapphire gradient appears in exactly four places. Its scarcity gives it weight. |

---

# 2. Colour System

## 2.1 Dark Theme (Default)

### Backgrounds

| Token | Value | Usage |
|:--|:--|:--|
| `--bg` | `#040A11` | Page background (deep navy) |
| `--bg-aura-1` | `rgba(16,185,129,0.18)` | Emerald radial glow |
| `--bg-aura-2` | `rgba(61,139,253,0.16)` | Sapphire radial glow |

### Surfaces (Glass Hierarchy)

| Token | Value | Opacity | Usage |
|:--|:--|:--|:--|
| `--surface` | `rgba(28,45,63,0.72)` | 72% | Primary containers: cards, sidebar, modals |
| `--surface-solid` | `#0E1926` | 100% | Fallback when blur is unsupported |
| `--surface2` | `rgba(38,58,79,0.68)` | 68% | Recessed areas: inputs, table headers, sub-panels |
| `--surface3` | `rgba(48,70,93,0.60)` | 60% | Tertiary depth: nested controls, file buttons |

### Borders & Rims

| Token | Value | Usage |
|:--|:--|:--|
| `--border` | `rgba(199,234,225,0.13)` | Default border on all containers |
| `--border2` | `rgba(199,234,225,0.22)` | Hover state border, scrollbar thumb |
| `--rim` | `rgba(199,234,225,0.24)` | Inset `box-shadow` top highlight (glass edge) |

### Text Hierarchy

| Token | Value | Usage |
|:--|:--|:--|
| `--text-strong` | `#FFFFFF` | Headings, stat numbers, emphasis |
| `--text` | `#E4EFF1` | Body text, primary content |
| `--text2` | `#9FB6BF` | Secondary text, labels, descriptions |
| `--text3` | `#6E8794` | Tertiary text, metadata, placeholders |
| `--mint` | `#C7EAE1` | Brand accent text (used sparingly) |

### Accent & Interactive

| Token | Value | Usage |
|:--|:--|:--|
| `--accent` | `#3D8BFD` | Interactive elements, focus rings, active states |
| `--accent2` | `#7DB2FF` | Lighter accent for hover text, link emphasis |
| `--accent-glow` | `rgba(61,139,253,0.16)` | Focus ring glow, active nav background |
| `--accent-soft` | `rgba(61,139,253,0.12)` | Active nav-link background |
| `--accent-soft2` | `rgba(61,139,253,0.16)` | Toggle active state, selected row |
| `--accent-row` | `rgba(61,139,253,0.06)` | Table row hover highlight |
| `--accent-border` | `rgba(61,139,253,0.34)` | Active segmented control border |

### Semantic Colours

| Semantic | Solid | Background | Border | Border Strong |
|:--|:--|:--|:--|:--|
| **Green** (success, active) | `#10B981` | `rgba(16,185,129,0.12)` | `rgba(16,185,129,0.30)` | `rgba(16,185,129,0.42)` |
| **Red** (error, danger) | `#F2555F` | `rgba(242,85,95,0.12)` | `rgba(242,85,95,0.30)` | `rgba(242,85,95,0.42)` |
| **Amber** (warning, pending) | `#F5A524` | `rgba(245,165,36,0.12)` | — | — |
| **Blue** (info, completed) | `#3D8BFD` | `rgba(61,139,253,0.12)` | — | — |
| **Pink/Purple** | `#B98CF0` | — | — | — |
| **Cyan** | `#22D3EE` | — | — | — |

---

## 2.2 Light Theme

Activated via `data-theme="light"` on the root html element.

### Key Differences

| Token | Dark Value | Light Value |
|:--|:--|:--|
| `--bg` | `#040A11` | `#F2F7F7` (mint-tinted white) |
| `--surface` | `rgba(28,45,63,0.72)` | `rgba(255,255,255,0.78)` |
| `--surface-solid` | `#0E1926` | `#FFFFFF` |
| `--surface2` | `rgba(38,58,79,0.68)` | `rgba(236,244,245,0.80)` |
| `--surface3` | `rgba(48,70,93,0.60)` | `rgba(222,235,237,0.85)` |
| `--border` | `rgba(199,234,225,0.13)` | `rgba(16,58,66,0.11)` |
| `--rim` | `rgba(199,234,225,0.24)` | `rgba(255,255,255,0.75)` |
| `--text` | `#E4EFF1` | `#14262E` |
| `--text-strong` | `#FFFFFF` | `#071820` |
| `--text2` | `#9FB6BF` | `#4C6570` |
| `--text3` | `#6E8794` | `#6B838E` |
| `--mint` | `#C7EAE1` | `#2E7F72` |
| `--accent` | `#3D8BFD` | `#1668D6` |
| `--green` | `#10B981` | `#047857` |
| `--red` | `#F2555F` | `#C62F3B` |
| `--shadow` | `0 8px 32px rgba(0,0,0,0.42)` | `0 8px 32px rgba(12,40,48,0.10)` |

> **Design rule:** Light theme is *mint-tinted white*, never neutral grey. The same brand rim (`--rim`) carries across both themes so the identity remains cohesive.

---

# 3. Typography

## 3.1 Typeface

| Role | Font | Fallback Stack |
|:--|:--|:--|
| **Primary** | **IBM Plex Sans** | `-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif` |
| **Monospace** (code/data) | System monospace | `'SF Mono', Consolas, monospace` |

**Why IBM Plex Sans:** Medical-grade clarity. Designed for long-form reading in technical contexts. Open-source (SIL OFL). Its tabular figures align financial and clinical data naturally.

### Weight Scale

| Weight | CSS Value | Usage |
|:--|:--|:--|
| Light | `300` | Decorative large text (rarely used) |
| Regular | `400` | Body text, descriptions, table cells |
| Medium | `500` | Labels, secondary headings, nav links |
| Semi-Bold | `600` | Card headings, page titles, buttons |
| Bold | `700` | Login title, sidebar brand name |
| Extra-Bold | `800` | Stat card numbers (legacy; now `600`) |

## 3.2 Type Scale

| Element | Size | Weight | Tracking | Line Height | Colour |
|:--|:--|:--|:--|:--|:--|
| Page title (h2) | `1.6rem` | 600 | `-0.025em` | 1.2 | `--text-strong` |
| Card heading (h3) | `1rem` | 600 | `-0.018em` | 1.4 | `--text-strong` |
| Login title | `1.35rem` | 700 | `-0.03em` | 1.3 | `--text-strong` |
| Sidebar brand | `1.1rem` | 700 | default | 1.3 | `--text` |
| Nav link | `0.88rem` | 500 | default | 1.5 | `--text2` |
| Body text | `0.92rem` | 400 | default | 1.6 | `--text` |
| Button | `0.88rem` | 600 | default | 1 | varies |
| Small button | `0.78rem` | 600 | default | 1 | varies |
| Table header | `0.72rem` | 700 | `0.06em` | 1.3 | `--text3` |
| Table cell | `0.87rem` | 400 | default | 1.4 | `--text` |
| Badge | `0.73rem` | 600 | `0.3px` | 1 | semantic colour |
| Stat label | `0.75rem` | 500 | `0.08em` | 1.3 | `--text3` |
| Stat number | `2rem` | 600 | `-0.03em` | 1 | `--text-strong` |
| Meta/subtitle | `0.85rem` | 400 | default | 1.5 | `--text3` |

### Typographic Features

```css
body { letter-spacing: -0.011em; }

.stat .num, .usage-metric .val, td, th {
    font-variant-numeric: tabular-nums;
    font-feature-settings: "tnum" 1;
}

-webkit-font-smoothing: antialiased;
-moz-osx-font-smoothing: grayscale;
```

---

# 4. Spatial System

## 4.1 Border Radius Hierarchy

Radius increases with elevation — the further from the page, the softer the edges.

| Token | Value | Applied To |
|:--|:--|:--|
| `--radius` | `14px` | Stat cards (closest to page) |
| `--radius-lg` | `18px` | Cards, form cards (mid-level) |
| `--radius-xl` | `22px` | Modals, login card (top-level, floating) |
| — | `10px` | Inputs, search bars, buttons, nav links |
| — | `20px` | Badges (pill shape) |
| — | `8px` | Small buttons, nested controls |

## 4.2 Spacing Grid

All spacing follows a **4px base unit**:

| Unit | Value | Usage |
|:--|:--|:--|
| 1u | 4px | Minimal gaps (badge padding) |
| 2u | 8px | Compact gap (grid gaps, row spacing) |
| 3u | 12px | Standard control gap (button groups, field spacing) |
| 4u | 16px | Card padding-component, stat grid gap |
| 5u | 20px | Card-to-card margin, modal action margin |
| 6u | 24px | Card inner padding, sidebar head padding |
| 7u | 28px | Page head margin bottom, form card padding |
| 8u | 32px | Main content padding, login card padding |
| 9u | 36px | Main content side padding |

## 4.3 Layout Constants

| Property | Value | Notes |
|:--|:--|:--|
| Sidebar width | `260px` | Fixed, collapsible on mobile |
| Main content max-width | `1400px` | Applied at >=1441px |
| Login card max-width | `420px` | Centered on viewport |
| Modal max-width | `520px` | Max-height 90vh with scroll |
| Toast max-width | `380px` | Fixed top-right |
| Stat card min-width | `180px` | Flex-basis, max 300px |

---

# 5. Surface & Glass Model

## 5.1 Glass Recipe

Every container in the product follows one compositing formula:

```css
.container {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-*);
    backdrop-filter: blur(var(--blur)) saturate(155%);
    box-shadow: inset 0 1px 0 var(--rim), var(--shadow);
}
```

### Glass Components

| Glass Surface | Background | Blur | Shadow |
|:--|:--|:--|:--|
| Stat card | --surface (72%) | 20px + saturate(155%) | inset rim + --shadow |
| Card / Form card | --surface (72%) | 20px + saturate(155%) | inset rim + --shadow |
| Modal | --surface (72%) | 20px + saturate(155%) | inset rim + --shadow-lg |
| Toast | --surface (72%) | 20px + saturate(155%) | inset rim + --shadow-lg |
| Sidebar | --surface (72%) | 20px + saturate(155%) | None (bordered right) |
| Login card | --surface (72%) | 20px + saturate(155%) | inset rim + --shadow-lg |
| Ghost button | — | 8px | — |

### Fallback Strategy

```css
@supports not ((backdrop-filter: blur(4px)) or (-webkit-backdrop-filter: blur(4px))) {
    :root {
        --surface: #0E1926;
        --surface2: #17242F;
        --surface3: #1E3044;
    }
}
```

> Browsers without backdrop-filter get opaque surfaces so text never sits on a see-through panel.

## 5.2 Shadow Tokens

| Token | Value | Context |
|:--|:--|:--|
| --shadow | 0 8px 32px rgba(0,0,0,0.42) | Cards, stats, form cards |
| --shadow-lg | 0 24px 64px rgba(0,0,0,0.55) | Modals, login, toasts |
| Button shadow | 0 4px 18px rgba(16,185,129,0.20), 0 2px 10px rgba(61,139,253,0.22) | .btn-accent only |
| Logo mark shadow | 0 10px 34px rgba(16,185,129,0.22), 0 4px 14px rgba(61,139,253,0.24) | Login logo container |

---

# 6. Background & Aura System

The page never has a flat background. Two **radial gradient auras** diffuse through the glass surfaces, creating depth and environmental lighting.

## 6.1 Login Screen Background

```css
.login-screen {
    background:
        radial-gradient(ellipse 70% 55% at 18% 42%, var(--bg-aura-1), transparent 62%),
        radial-gradient(ellipse 65% 50% at 82% 18%, var(--bg-aura-2), transparent 60%),
        var(--bg);
}
```

- **Aura 1** (emerald, bottom-left): Creates warmth and "life"
- **Aura 2** (sapphire, top-right): Creates cool intelligence

## 6.2 Main Content Background

```css
.main {
    background:
        radial-gradient(ellipse 60% 45% at 88% -5%, var(--bg-aura-2), transparent 58%),
        radial-gradient(ellipse 55% 40% at 5% 8%, var(--bg-aura-1), transparent 55%),
        var(--bg);
    background-attachment: fixed;
}
```

## 6.3 Owner Panel Login

```css
.login-overlay {
    background: radial-gradient(circle at 50% 28%, var(--bg-aura-2) 0%, rgba(5,11,18,0.95) 68%);
    backdrop-filter: blur(16px);
}
```

---

# 7. Component Library

## 7.1 Buttons

### Primary Action (btn-accent)

```css
.btn-accent {
    background: var(--brand-grad);
    color: #fff;
    padding: 12px 24px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 0.88rem;
    box-shadow: 0 4px 18px rgba(16,185,129,0.20),
                0 2px 10px rgba(61,139,253,0.22),
                inset 0 1px 0 rgba(255,255,255,0.24);
}
/* Hover: lifts 1px, intensifies glow */
/* Active: scale(0.97) */
```

### Ghost Button (btn-ghost)

```css
.btn-ghost {
    background: var(--surface2);
    color: var(--text2);
    border: 1px solid var(--border);
    backdrop-filter: blur(8px);
}
/* Hover: border=accent, color=accent2, bg=accent-glow */
```

### Semantic Buttons

| Variant | Background | Text | Border |
|:--|:--|:--|:--|
| btn-red | --red-bg | --red | --red-border |
| btn-green | --green-bg | --green | --green-border |

### Button Sizes

| Size | Padding | Font Size | Radius |
|:--|:--|:--|:--|
| Default | 12px 24px | 0.88rem | 10px |
| Small (btn-sm) | 7px 16px | 0.78rem | 8px |

## 7.2 Form Controls

### Text Input / Select / Textarea

```css
.field input, .field select, textarea {
    padding: 12px 16px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: var(--text);
    font-size: 0.92rem;
}
/* Focus: border=accent, shadow=0 0 0 3px accent-glow */
/* Hover: border=border2 */
/* Placeholder: text3 */
```

### Custom Checkbox / Radio

- Appearance stripped (appearance: none)
- 18x18px, border-radius: 5px (checkbox) or 50% (radio)
- Unchecked border: --text3 (WCAG 1.4.11 compliant: 3:1 against --surface2)
- Checked: background=accent, checkmark via clip-path polygon
- Focus: 0 0 0 3px accent-glow

### Toggle Switch

```css
.switch .track {
    width: 46px; height: 26px;
    border-radius: 13px;
    background: var(--border2);
}
/* Checked: background=#10B981 (green) */
/* Knob: 20x20px white circle, translateX(20px) on check */
```

### Day Picker (Week Selector)

- Chips: 54px min-width, 9px 12px padding, border-radius: 9px
- Selected: background=accent, color=#fff

## 7.3 Cards

### Standard Card

```css
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);       /* 18px */
    padding: 22px 24px;
    margin-bottom: 18px;
    backdrop-filter: blur(20px) saturate(155%);
    box-shadow: inset 0 1px 0 var(--rim), var(--shadow);
}
/* Hover: border-color=border2 */
```

### Stat Card

```css
.stat {
    border-radius: var(--radius);           /* 14px */
    padding: 24px 22px;
    flex: 1 1 180px;
    max-width: 300px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
}
/* Top edge: 1px brand gradient at 50% opacity */
/* Hover: translateY(-3px), border=border2 */
```

### Form Card

Same glass treatment as .card, but padding: 28px.

## 7.4 Tables

```css
th {
    padding: 12px 18px;
    font-size: 0.72rem;
    font-weight: 700;
    color: var(--text3);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
}

td {
    padding: 13px 18px;
    font-size: 0.87rem;
    font-variant-numeric: tabular-nums;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
}

/* Row hover: background=accent-row (6% blue) */
/* Last row: no bottom border */
```

## 7.5 Badges

All badges share this structure:

```css
.badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 12px;
    border-radius: 20px;        /* pill shape */
    font-size: 0.73rem;
    font-weight: 600;
    letter-spacing: 0.3px;
}
.badge::before {
    content: '';
    width: 6px; height: 6px;
    border-radius: 50%;         /* status dot */
}
```

| Badge | Background | Text | Dot |
|:--|:--|:--|:--|
| Confirmed / Active / Sent | --green-bg | --green | --green |
| Cancelled / Failed | --red-bg | --red | --red |
| Completed | --blue-bg | --blue | --blue |
| No-show / Pending | --amber-bg | --amber | --amber |
| Inactive | --muted-soft | --text3 | --text3 |

## 7.6 Modals

```css
.modal-overlay {
    background: rgba(2,6,12,0.62);
    backdrop-filter: blur(6px);
}

.modal {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-xl);    /* 22px */
    padding: 32px;
    max-width: 520px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: inset 0 1px 0 var(--rim), var(--shadow-lg);
    animation: fadeUp 0.3s ease;
}
```

### Modal Header

```css
.modal h3 {
    font-size: 1.1rem;
    font-weight: 700;
    letter-spacing: -0.018em;
    color: var(--text-strong);
    padding-bottom: 14px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 22px;
}
```

## 7.7 Toasts

```css
.toast {
    padding: 14px 16px;
    border-radius: 12px;
    font-size: 0.86rem;
    font-weight: 500;
    background: var(--surface);
    box-shadow: inset 0 1px 0 var(--rim), var(--shadow-lg);
}

.toast-ok { border: 1px solid var(--green-border-strong); }
.toast-err { border: 1px solid var(--red-border-strong); }
```

- **Position:** Fixed top: 20px; right: 20px;
- **Entry animation:** translateX(30px) to translateX(0) in 0.25s
- **Exit animation:** reverse in 0.2s

## 7.8 Navigation

### Sidebar Nav Link

```css
.nav-link {
    padding: 11px 14px;
    border-radius: 10px;
    color: var(--text2);
    font-size: 0.88rem;
    font-weight: 500;
}

.nav-link.on {
    background: var(--accent-soft);
    color: var(--text-strong);
}

/* Active indicator: 3px x 20px brand gradient bar, left edge, vertically centered */
.nav-link.on::before {
    width: 3px; height: 20px;
    border-radius: 0 3px 3px 0;
    background: var(--brand-grad);
}
```

### Toggle Group (Segmented Control)

```css
.toggle-group {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
}

.toggle-btn {
    padding: 8px 18px;
    font-size: 0.82rem;
    font-weight: 600;
}

.toggle-btn.on {
    background: var(--accent-soft2);
    color: var(--text-strong);
}
```

## 7.9 Search Bar

```css
.search-bar {
    padding: 12px 18px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    font-size: 0.92rem;
}
/* Focus: border=accent, glow=3px accent-glow */
/* With icon: padding-left: 42px, icon at left: 15px */
```

## 7.10 Alerts

```css
.alert {
    padding: 12px 16px;
    border-radius: 10px;
    font-size: 0.85rem;
}

.alert-ok  { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }
.alert-err { background: var(--red-bg);   color: var(--red);   border: 1px solid var(--red-border); }
```

## 7.11 Progress Bars

### Usage Bar

```css
.usage-bar-track { background: var(--bg3); border-radius: 8px; height: 12px; }
.usage-bar-fill  { background: var(--brand-grad); border-radius: 8px; transition: width 0.8s; }
.usage-bar-fill.over { background: linear-gradient(90deg, var(--red), var(--amber)); }
```

### Department Bar

```css
.dept-track { height: 8px; background: var(--surface2); border-radius: 4px; }
.dept-fill  { border-radius: 4px; transition: width 0.6s; }
/* Each bar gets a unique gradient pair for visual differentiation */
```

## 7.12 Scrollbar

```css
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--accent); }
```

---

# 8. Iconography

## 8.1 System

All icons are **inline SVG symbols** defined once in a hidden svg block and referenced via use href="#i-name".

### Properties

| Property | Value |
|:--|:--|
| Grid | 24x24px viewBox |
| Display size | 16x16px (.i) or 20x20px (.i-lg) |
| Stroke | currentColor, 1.75px width |
| Caps/Joins | round |
| Fill | none (pure stroke icons) |

### Icon Set

| ID | Glyph | Used For |
|:--|:--|:--|
| i-activity | EKG line | Insights, analytics |
| i-bell | Bell | Notifications |
| i-building | Building | Clinics, organizations |
| i-card | Credit card | Payments |
| i-close | X | Close actions |
| i-download | Download arrow | Export |
| i-eye / i-eye-off | Eye | Password toggle |
| i-list | List | Menu, lists |
| i-lock | Padlock | Security, login |
| i-megaphone | Speaker | Broadcasts |
| i-pencil | Pencil | Edit |
| i-plus | Plus | Add/Create |
| i-refresh | Refresh arrows | Reload |
| i-search | Magnifying glass | Search |
| i-send | Paper plane | Send message |
| i-settings | Gear | Settings |
| i-shuffle | Shuffle arrows | Random/mix |
| i-trash | Bin | Delete |
| i-trending-up | Trend line | Growth charts |
| i-users | People | Staff, patients |
| i-wallet | Wallet | Billing |
| i-zap | Lightning bolt | Quick actions |

## 8.2 Usage Rules

1. **No emoji.** Every OS renders emoji differently; stroke SVGs are pixel-identical everywhere.
2. Icons inside buttons get margin-right: 7px. Standalone icons (close button, toggle) get 0.
3. Icons inherit currentColor — they automatically match the text/button colour.

---

# 9. Motion & Animation

## 9.1 Timing Tokens

| Token | Value | Usage |
|:--|:--|:--|
| --motion-fast | 150ms | Button press, hover transitions |
| --motion-base | 250ms | Page transitions, stagger sequences |
| --ease-out | cubic-bezier(0.16, 1, 0.3, 1) | Natural deceleration curve |

## 9.2 Animations

### Fade Up (Entry)

```css
@keyframes fadeUp {
    from { opacity: 0; transform: translateY(24px); }
    to   { opacity: 1; transform: translateY(0); }
}
/* Used: login card (0.5s), modals (0.3s) */
```

### Stagger Fade Up (Batch Entry)

```css
@keyframes staggerFadeUp {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
}
/* Children stagger at 40ms intervals (0, 40, 80, 120, 160, 200ms) */
/* Used: stat cards, grid items on section change */
```

### Fade In (Section Switch)

```css
@keyframes fadeIn {
    from { opacity: 0; }
    to   { opacity: 1; }
}
/* Duration: 0.3s. Used: .sec (tab content) */
```

### Shake (Error)

```css
@keyframes shake {
    20%, 60% { transform: translateX(-4px); }
    40%, 80% { transform: translateX(4px); }
}
/* Duration: 0.4s. Used: login error alerts */
```

### Pulse Dot (Live Indicator)

```css
@keyframes pulse-dot {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.4; }
}
/* Duration: 2s infinite. Used: sidebar "online" dot */
```

### Spinner

```css
@keyframes spin {
    to { transform: rotate(360deg); }
}
/* Duration: 0.6s linear infinite */
/* 30x30px ring, 3px stroke, --border base, --accent top */
```

### Toast Slide

```css
@keyframes toastIn {
    from { opacity: 0; transform: translateX(30px); }
    to   { opacity: 1; transform: translateX(0); }
}
/* Duration: 0.25s. Exit: reverse 0.2s */
```

## 9.3 Reduced Motion

```css
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.001ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.001ms !important;
    }
}
```

---

# 10. Responsive Framework

## 10.1 Breakpoints

| Breakpoint | Trigger | Key Changes |
|:--|:--|:--|
| >= 1441px | Large desktop | Main content capped at 1400px |
| <= 1100px | Narrow desktop | Chart grids collapse to single column |
| <= 1024px | Tablet landscape | Reduced main padding (28px 24px), stats 3-col |
| <= 768px | Tablet portrait | Sidebar collapses (hamburger appears), main full width, stats 2-col, form rows single column |
| <= 560px | Small phone | Checkbox grids single column |
| <= 480px | Narrow phone | Stats single column |

## 10.2 Sidebar Behaviour

| State | CSS |
|:--|:--|
| Default (> 768px) | width: 260px, position: fixed, always visible |
| Collapsed (<= 768px) | transform: translateX(-100%) |
| Open on mobile | .mobile-open restores translateX(0) + backdrop overlay |

## 10.3 Hamburger Button

```css
.hamburger {
    display: none;
    position: fixed;
    top: 14px; left: 14px;
    z-index: 200;
    width: 44px; height: 44px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
}
/* Visible at <= 768px */
```

---

# 11. Accessibility

## 11.1 Focus Management

```css
:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
    border-radius: 4px;
}

button:focus-visible, .nav-link:focus-visible, .btn:focus-visible {
    outline-offset: 3px;
}

input:focus { box-shadow: 0 0 0 3px var(--accent-glow); }
```

## 11.2 Contrast Ratios

| Pair | Ratio | Standard |
|:--|:--|:--|
| --text (#E4EFF1) on --bg (#040A11) | **15.2:1** | AAA |
| --text2 (#9FB6BF) on --surface | **6.8:1** | AA |
| --text3 (#6E8794) on --surface | **4.2:1** | AA (large text) |
| --accent (#3D8BFD) on --bg | **5.1:1** | AA |
| White on --brand-grad midpoint | **4.6:1** | AA |

## 11.3 Colour-Scheme Declaration

```css
:root { color-scheme: dark; }
:root[data-theme="light"] { color-scheme: light; }
```

This ensures native browser widgets (date pickers, selects, spinners) match the panel's theme.

## 11.4 Checkbox Contrast

```css
/* Unchecked border uses --text3 (not --border2) for 3:1 minimum contrast
   against the field background, per WCAG 1.4.11 Non-text Contrast. */
input[type="checkbox"], input[type="radio"] {
    border: 1.5px solid var(--text3);
}
```

## 11.5 Disabled States

```css
input:disabled, button:disabled {
    opacity: 0.45;
    cursor: not-allowed;
    pointer-events: none;
}
```

---

# 12. Application to Products

This design system is the **canonical visual language** for every product in the Kriya AI suite.

## 12.1 Product Panels

| Product | Primary Panel | Notes |
|:--|:--|:--|
| **Hospital Admin** | admin/index.html | Full design system with light/dark toggle |
| **Platform Owner** | admin/platform.html | Dark only, identical tokens |
| **Corporate Viewer** | Admin panel (restricted) | Same glass surfaces, single-page view |
| **Phlebotomist Portal** | Admin panel (restricted) | Mobile-first card layout, same tokens |

## 12.2 Extending to New Products

When building a new Kriya AI product panel:

1. **Import the font:**
   ```html
   <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
   ```

2. **Copy the `:root` token block** from this specification (section 2.1 for dark, 2.2 for light).

3. **Copy the glass recipe** (section 5.1) — backdrop-filter, box-shadow inset rim, and the @supports fallback.

4. **Copy the icon sprite** — the svg block with all symbol definitions.

5. **Follow the radius hierarchy:**
   - Inline elements: 8-10px
   - Page-level cards: 14-18px
   - Floating overlays: 20-22px

6. **Respect the gradient rule:** The --brand-grad appears in at most 4 places. If your new panel needs a primary CTA, it gets the gradient. Everything else uses --accent as a solid colour.

7. **Apply the aura background** on the main content area (section 6.2). This is what makes the glass surfaces come alive.

## 12.3 Theme Switching

```javascript
function toggleTheme() {
    const root = document.documentElement;
    const current = root.getAttribute('data-theme');
    const next = current === 'light' ? null : 'light';
    if (next) root.setAttribute('data-theme', next);
    else root.removeAttribute('data-theme');
    localStorage.setItem('kriya-theme', next || 'dark');
}
```

## 12.4 Design Tokens as CSS Variables

All tokens are defined on :root and overridden via [data-theme="light"]. No build step, no preprocessor — pure CSS custom properties. This ensures:

- **Zero-dependency theming** — any HTML page can use the system
- **Runtime switching** — theme changes are instant, no page reload
- **Inheritance** — components automatically inherit their container's theme

---

> **Document Owner:** Kriya AI Engineering
> **Classification:** Internal — All Rights Reserved
> **Last Updated:** October 2026

# Serin Design System — "Calm Dashboard" (v0.6)

The single source of truth for Serin's visual language, applied in
[`frontend/src/styles.css`](../frontend/src/styles.css). The handoff brief
that drove this redesign lives at [`DESIGN-BRIEF.md`](DESIGN-BRIEF.md).
The previous "Swiss Terminal" system (cream canvas, DM Sans + JetBrains
Mono, dense) is superseded.

## Direction

A calm, premium-feeling portfolio dashboard. Soft white cards on a cool
off-white canvas, a single humanist sans (Manrope) with tabular numerals
everywhere, gentle gradient area charts, iOS-style segmented controls. One
restrained blue for brand, links, CTAs and the active-state pill. Green
and red are **semantic only** (gains / losses / success / errors), never
decorative.

## Tokens

All tokens are CSS custom properties on `:root` so any new component
picks them up automatically.

### Color

```
--bg     #f3f4f7   page canvas (cool off-white)
--card   #ffffff   cards / panels
--ink    #181b22   primary text
--sec    #5c636e   secondary text
--mut    #9097a1   muted labels and captions
--bd     #e9eaee   hairline borders
--inset  #f3f4f7   table heads, chips, input wells, segmented tracks
--acc    #2f6bed   the accent: CTAs, links, active states, brand mark
--up     #15a05a   gains / success
--down   #e0453a   losses / errors
```

Accent-tinted helpers used in a few places:

```
accent wash (pills, selected style cards)  #eaf0fe / #f5f8ff
gain wash (status pills, "ok" toasts)       #e7f6ee
donut neutral ramp (cash → smallest)        #cdd3dc #7d8aa0 #95a1b4 #aeb8c7 #c4ccd7 #d7dde4 #e6eaee
```

The largest donut slice uses `--acc`; all other slices use the neutral
gray ramp. Green/red appear only on semantic values, never as decorative
fills.

### Typography

- **Family:** `'Manrope', -apple-system, system-ui, sans-serif` — Google Fonts, weights 400/500/600/700/800. **One typeface for everything; no monospace.**
- **Numbers:** every figure carries `font-variant-numeric: tabular-nums` (utility class `.num` plus baked into number cells, KPI values, big prices, table cells, tax-lot rows).
- **Smoothing:** `-webkit-font-smoothing: antialiased`.

Type scale actually used (px / weight / letter-spacing):

```
Display number (KPI, price)     30–34 / 800 / -0.02em
Section / card title            15–16 / 800 / -0.01em
Screen title (Connectors)       28    / 800 / -0.025em
Briefing title                  27    / 800 / -0.025em
Stat value                      20    / 700 / -0.01em
Body / news title               15    / 700
Row / nav / button              13.5–14 / 600–700
Label / caption / sublabel      12–13 / 600 (color --mut)
Micro label (table head)        11.5  / 700 / +0.02em, color --mut
```

### Spacing & layout

- 8 px base rhythm. Page padding `26px 30px 70px`. Content max-width **1280 px**, centered.
- Card grid gaps: **16–18 px**. Card padding: **20–24 px**.
- KPI row: 4 equal columns (`repeat(4, 1fr)`, gap 16).
- Lower region: two columns `1.66fr 1fr` (positions / allocation) or `1.7fr 1fr` (briefings reader / sidebar). News & Connectors: `1fr 1fr`.

### Radius & shadow

```
Card radius          18 px
Buttons / inputs     9–11 px
Chips / controls     8 px
Pills (status)       999 px
Card shadow          0 1px 2px rgba(20,24,34,.04), 0 4px 14px rgba(20,24,34,.04)
CTA shadow           0 1px 2px rgba(47,107,237,.25)
Segmented active     0 1px 3px rgba(20,24,34,.12)
```

### Motion

Toggles and segmented pills transition `background` / `left` at ~150 ms ease. Keep it minimal; respect `prefers-reduced-motion`.

## Global chrome (all screens)

**Top bar** — flex, margin-bottom 28 px:
- Brand mark (three ascending rounded bars in `--acc`) + `serin` wordmark (20 px / 800) + faint timestamp (12.5 px, `--mut`).
- Tab nav, items 14 px / 600 in `--sec`, padding `8 13`, radius 9. Active pill is `#e7e9ef` with `--ink` text. **Connectors** tab is tinted `--acc` even when inactive (it's the hero).
- Right side: ghost buttons (white, 1px `--bd` border, radius 11) and a primary `+ Add position` (`--acc` bg, white, radius 11, CTA shadow).

Every interactive text has `white-space: nowrap`.

## Components

**`.card` / `.panel`** — white background, 1 px `--bd` border, 18 px radius, soft card shadow.

**`.kpi`** — KPI card. 22 24 padding. Label 12.5 px / 600 `--mut`. Value 30 px / 800 with `.up` / `.down` color modifiers. Sub-line 13 px `--sec`.

**`.segmented`** — iOS-style range selector. 2-px gap children on `#eef0f3` track at 11 px radius, active child `#fff` with the pill shadow.

**`.chart`** — viewBox `0 0 1000 280`, `preserveAspectRatio="none"`. Line is `--up` or `--down`, 2.4 stroke, non-scaling. Area uses the linear-gradient defs (`#fg` for up, `#fgd` for down) at .22 → 0 vertical opacity. Three faint gridlines at #f1f2f5.

**Positions table** — 8-column CSS grid:
```
minmax(150px,1.8fr) minmax(74px,auto) minmax(48px,.82fr)
minmax(48px,.82fr) minmax(58px,.95fr) minmax(74px,1.1fr)
minmax(58px,.95fr) 88px
```
Rows 56 px, hairline separators, hover `--inset`. Numbers right-aligned, tabular, nowrap.

**Donut** — `<circle r="68" stroke-width="22">` per segment with computed `stroke-dasharray` / `stroke-dashoffset`, rotated −90°. Center label is `dval` (18/800 tabular) over `dlab` (10.5/700 muted uppercase).

**Connector card (`.connector-card` / `.conncard`)** — 46×46 monogram tile (`--acc` letter on `--inset`), name (16/800) + status pill, description (13.5 `--sec`), iOS toggle switch (40×24 track, green when on), Configure/Test/Docs ↗ links. Expanded config form auto-rendered from the manifest schema.

**Status pill (`.statuspill`)** — 11.5 px / 700, 999 radius. `.on` (green wash), `.pending` (blue wash), `.off` (gray).

**Switch** — 40 × 24 track (radius 999, `#d3d7df` off / `--up` on), 20 px white thumb that animates left → right at 150 ms.

**Briefing reader (`.reader`)** — 30 34 padding. Kicker `.rbadge` (✦ AI BRIEFING in blue wash) + meta. Title 27 px / 800. Intro 15.5 px / 500 `--sec`. Sections (`.rsection`) have an uppercase `.rsh` heading with a 7 px `--acc` square dot, then `.bullet` rows (gray dot + `--sec` text with bold `--ink` lead).

**News item (`.nitem`)** — `<a>` link, 16 24 padding, hover `--inset`. Meta row has source (`--ink` 700) + time (`--mut` 500), optional ticker chip (blue wash). Title 15 px / 700. Summary 13.5 px `--sec`.

## Assets

- **Brand mark** — three ascending rounded bars (`<rect>` x 3, 10, 17 / y 13, 8, 3 / w 4 / h 8, 13, 18 / rx 1.2), filled `--acc`. Lives at:
  - [`frontend/public/favicon.svg`](../frontend/public/favicon.svg)
  - [`frontend/public/icon-192.svg`](../frontend/public/icon-192.svg)
  - [`frontend/public/icon-512.svg`](../frontend/public/icon-512.svg)
  - Inline in App.jsx via `<BrandMark />`.
- **Fonts** — Manrope via Google Fonts, weights 400 – 800, imported at the top of `styles.css`.
- No icon library; existing inline SVG icons are kept.

## Migration log

- 2026-06-23 — original "Swiss Terminal" system (cream + DM Sans + JetBrains Mono).
- 2026-06-23 — handoff brief [`DESIGN-BRIEF.md`](DESIGN-BRIEF.md) prepared.
- 2026-06-24 — "Calm Dashboard" applied: tokens replaced in `styles.css`, Manrope adopted, brand mark switched to three-bar chart in `--acc`, Connectors hero card surface upgraded, OSS-pride chip bar added, Briefings / News / Stocks restyled to the new system. Verified in browser.

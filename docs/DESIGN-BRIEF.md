# Serin — Design Handoff Brief

**Purpose.** A self-contained handoff so a designer (human, a fresh Claude
conversation, or a design tool) can pick up Serin's UI work without re-reading
the source conversation. Pair this with the existing
[DESIGN.md](DESIGN.md) (the current visual system) and screenshots of the
running app.

---

## 1. The product in one paragraph

Serin is an **open-source, self-hosted portfolio workspace**. It tracks
holdings across brokerages (manual / CSV / SnapTrade), shows allocation and
performance, and optionally generates an AI daily briefing. The defining
feature is the **connector platform** — the data layer is a plugin system, and
the Connectors portal renders config forms automatically from each plugin's
manifest schema. Adding a data source = a 40-line module + a PR; no UI work
needed.

## 2. Strategy context

- **North Star (2026-06-23):** *the most extensible open-source portfolio
  tracker — connect any broker, any data source, any format.* No
  commercialization in the near term.
- **Health metrics:** GitHub stars, contributors, **number of connectors**
  (especially community-contributed), "good first issue" success.
- **Target user:** DIY investors with positions across multiple brokerages,
  basic technical comfort (`docker compose up`), privacy-curious but not
  paranoid. Often developers, designers, tech-adjacent professionals.
- **Voice:** confident, deliberate, "context not advice." Open-source pride
  without scrappiness. Indie but never amateurish.

## 3. Information architecture

Five tabs in the current build:

1. **Overview** — total value, day change, trend chart, allocation donut, positions table
2. **Stocks** — per-symbol detail (chart, performance metrics, allocation treemap, news, tax lots)
3. **Briefings** — AI briefing reader + scheduler + history
4. **News** — market headlines + ticker-matched portfolio news
5. **Connectors** — the data-layer plugin portal ← *the hero screen*

## 4. Current visual system (being redesigned)

The current design is documented in [DESIGN.md](DESIGN.md) as the "Swiss
Terminal" system. Summary:
- DM Sans (sans) + JetBrains Mono (mono), via Google Fonts
- Warm cream background (#f3f0e8) · white cards · accent green (#2eb887) ·
  accent red (#d2493d) · amber + blue accents · slate brand (#17231f)
- Inline `BrandMark` SVG logo (slate / cream / green / amber)
- Desktop-first, ~960 px content max-width
- **No UI library** — pure React + hand-written CSS; SVG visualizations
  hand-coded

## 5. Codebase pointers (where to actually edit)

| Layer | Path |
|---|---|
| App shell | `frontend/src/App.jsx` |
| Styles | `frontend/src/styles.css` |
| Connectors portal (newest, hero) | `frontend/src/components/Connectors.jsx` |
| Portfolio trend / sparkline / donut | `frontend/src/components/Charts.jsx` |
| Positions table + modals | `frontend/src/components/Positions.jsx` |
| Stock detail | `frontend/src/components/StockDetail.jsx`, `StockChart.jsx` |
| Allocation treemap | `frontend/src/components/Treemap.jsx` |
| Period returns | `frontend/src/components/PerformanceMetrics.jsx` |
| Briefings reader | `frontend/src/components/Briefings.jsx` |
| News feed | `frontend/src/components/News.jsx` |
| Broker connections | `frontend/src/components/Connections.jsx` |
| Brand assets | `frontend/public/{favicon,icon-192,icon-512}.svg`, `manifest.webmanifest` |

## 6. Constraints

- **No telemetry.** Don't propose anything that requires phoning home.
- **Open source forever.** Visual treatments that brag about it are fine;
  ones that depend on closed assets are not.
- **PWA-installable.** Layout has to work as a phone home-screen app too.
- **React 18, no SSR, no Next.js.**
- Currently no UI library / no Tailwind / no CSS-in-JS. **Open to changing
  this — but flag it as a deliberate decision** if the redesign needs it.
- **Dark mode strongly preferred** (current build is light-only).

## 7. Proposed design principles

- **Indie but polished.** Linear / Vercel / Plausible / Cal.com territory —
  not "open-source 2010 forum."
- **Calm.** Most finance UIs scream. Be the opposite.
- **Information-dense without being cramped.** This is a tool, not a marketing
  page.
- **Open-source proud.** Show contributor count, link to GitHub, surface the
  "you can edit the code" affordance somewhere visible.
- **The Connectors tab is the hero.** Closer to Home Assistant's Integrations
  page or Airbyte's catalog than a settings panel.

## 8. Reference points

| Pull these up | Why |
|---|---|
| [Ghostfolio](https://ghostfol.io) | The OSS benchmark we're rivaling |
| [Linear](https://linear.app), [Vercel](https://vercel.com), [Plausible](https://plausible.io), [Cal.com](https://cal.com) | Modern indie/OSS dashboard aesthetic |
| [Home Assistant — Integrations](https://www.home-assistant.io/integrations/), [n8n](https://n8n.io), [Airbyte](https://airbyte.com) | Connector portal / plugin catalog UX |
| Robinhood, Public, Magnifi | What to **not** look like (consumer-flashy) |
| Bloomberg, Sharesight | What to **not** look like (cold / dense) |

## 9. Answer these before redesigning

A designer will ask first; answering now saves a round trip:

- [ ] What feels off — palette, typography, density, hierarchy, the brand
  mark, the spacing rhythm, the chart style, all of the above?
- [ ] Which **one screen** is most important to nail first? (Recommendation:
  Connectors. Runner-up: Overview, as the first impression.)
- [ ] Aesthetic direction: editorial/serif, technical/mono, warm-financial,
  brutalist-OSS, something else?
- [ ] Keep DM Sans + JetBrains Mono, or open to typographic change?
- [ ] Keep cream/green/amber, or fresh palette?
- [ ] Light-only, dark-only, or system-default with both?

## 10. Screenshots to attach when handing off

Capture from a running `npm run app:local`:
- `artifacts/screenshots/connectors.png` — v0.4 Connectors portal (hero)
- `artifacts/screenshots/overview.png` — dashboard
- `artifacts/screenshots/stocks.png` — drill-in
- `artifacts/screenshots/briefings.png` — reader
- `artifacts/screenshots/news.png` — feed

## 11. Handoff options

**Option A — Fresh Claude conversation (claude.ai web)**
Paste this brief + the screenshots, then prompt:
> *"I want to redesign Serin's UI. Here is the brief + current-state
> screenshots. Use the ui-ux-pro-max skill if available. Start with the
> Connectors screen and give me three distinct visual directions before
> drilling into one."*

**Option B — A human designer (Figma, etc.)**
Send this brief + the GitHub repo URL. They have everything needed.

**Option C — Continue in this kind of session (Claude Code)**
The `ui-ux-pro-max` design skill is available here; we can invoke it
inline with this brief and start iterating against the actual code.

## 12. Definition of done for the redesign

- A coherent visual system (tokens for color / spacing / type) documented in
  CSS variables (`styles.css`) and a refreshed `DESIGN.md`
- The five primary screens redesigned
- Dark mode supported
- Mobile/PWA layouts tested at 375 px and 768 px viewports
- A short component-conventions section so the next contributor doesn't
  reverse-engineer the look

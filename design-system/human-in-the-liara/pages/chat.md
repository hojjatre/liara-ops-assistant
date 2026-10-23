# Page override: Chat (Chainlit UI)

> Overrides `../MASTER.md` for the assistant's chat screen. Anything not listed here follows MASTER.

## Language & direction
- UI copy is Persian; message bodies, cards and the status panel are `dir="rtl"`. Code, URLs, app names,
  numbers with units and API details stay `dir="ltr"`.
- Font stack: `Plus Jakarta Sans` (Latin) → `Vazirmatn` (Persian) → system sans; `JetBrains Mono` for code.
  Plus Jakarta Sans has no Arabic-script glyphs, so Vazirmatn is the effective Persian body font.
- Line height 1.7–1.85 for Persian body text.

## Theme
- Light by default (MASTER: "dark mode by default" is an anti-pattern for this product); a dark variant is
  provided for the theme toggle with the same roles (`public/theme.json`).

## Components (implemented as Chainlit custom elements in `public/elements/`)
| Component | Purpose | Notes |
|---|---|---|
| `ApprovalCard` | Human-in-the-loop approval for medium/high-risk tools | Risk shown by icon + text badge + colour (never colour alone); impact grid (downtime, cost, rollback, note); technical details collapsed; primary CTA uses the orange accent with dark text (≥4.5:1); read-only state after the decision |
| `SecretInput` | Private entry of env-var secrets | Password field with show/hide, paste and password managers allowed; value never posted to chat |
| `AlertCard` | Monitoring alert | Severity icon + label; one CTA "عیب‌یابی کن" |
| `StatusPanel` | Side panel | Per-app card with RAM/CPU meters (thresholds 80/90%), HTTP pill, monitoring state; recent audited operations timeline; team badge |

## Icons
- Lucide only (bundled with Chainlit); no emoji as icons anywhere in the UI or in assistant replies.

## Interaction
- All clickable elements: `cursor: pointer`, visible `:focus-visible` ring (primary blue), 150–300 ms colour
  transitions, no layout-shifting hover transforms; `prefers-reduced-motion` disables transitions.
- Buttons are at least 40 px tall; action rows wrap and stretch on screens under 480 px.

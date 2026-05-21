# H-Tracker Design System

Internal CRM-style tool for H-FARM College Global Partnerships.
Editorial / institutional feel — confident but understated. Closer to
the FT, McKinsey, or NYT product surfaces than to a typical SaaS dashboard.

## Brand mood

- **Authoritative, not corporate.** This is a tool that informs strategic
  decisions; it should feel substantive, not playful.
- **Editorial typography.** Crisp Helvetica Neue, generous letter-spacing
  on small caps eyebrows, sentence-case headings.
- **Warm paper background.** Off-white #FAFAF5 (not pure white) — easier on
  the eye for hours of use, signals "considered" rather than "tech demo".
- **Red as the urgent accent.** H-FARM's brand red (#D9534F) is reserved
  for primary actions, urgent badges, and the brand mark. Never decorative.
- **Navy/black as the structural color.** Used for sidebar, headers,
  primary text. Calm, never colored.
- **Gold as a sparing highlight.** Section eyebrows in navigation, hover
  states, "premium" KPI tiles (Critical / Up & Running). Restrained.

## Colors

### Primary palette
- **H-FARM Red** — `#D9534F` (primary actions, brand mark, urgent badges)
- **H-FARM Red Deep** — `#B0322E` (hover state on red)
- **H-FARM Red Soft** — `#FCE4E4` (red-tinted backgrounds, error notices)
- **Ink** — `#1A1A1A` (primary text, sidebar background, headings)
- **Ink Soft** — `#5A5A5A` (secondary text, meta info)
- **Ink Mute** — `#8A8A8A` (placeholder text, disabled states)

### Surfaces
- **Off-white** — `#FAFAF5` (page background — never use #FFFFFF for the canvas)
- **White** — `#FFFFFF` (cards, modals on top of off-white)
- **Paper** — `#F4F1E8` (input field backgrounds, paper-tone surfaces, code blocks)
- **Rule** — `#E5E2D6` (warm borders, dividers)
- **Rule Cool** — `#E6E6E6` (cool borders for cards inside paper surfaces)

### Brand accents
- **Brand Navy** — `#2D3150` (alternate dark; less common than Ink)
- **Brand Navy Deep** — `#1F2238`
- **Brand Navy Soft** — `#E3E4EC`

### Semantic — Priority bands
- **Critical** — `#8B1A1A` (deep maroon, most urgent)
- **Hot** — `#D9534F` (H-FARM red, active conversations)
- **Warm** — `#E0A93B` (amber, engaged interest)
- **Cold** — `#4A6B8A` (steel blue, stalled)
- **Cold-storage** — `#8E9AAB` (muted blue-grey, parked)
- **Up & Running** — `#2E7D5B` (forest green, live signed partnership)
- **Not interested** — `#6E6E6E` (neutral grey)

### Semantic — Strategic tiers
- **Digital Pioneer** — `#1F6FB4` (blue)
- **Prestige Hub** — `#C97A1A` (burnt orange)
- **Applied Leader** — `#2E7D5B` (green, same as Up & Running)
- **Established Partner** — `#6B4C7D` (deep purple)

## Typography

### Stack
```
font-family: 'Helvetica Neue', 'Segoe UI', -apple-system, sans-serif;
```
Monospace where used:
```
font-family: 'Consolas', monospace;  // phone numbers, IDs, technical strings
```

### Scale (sizes used in production)
- **Body** — 14px / 1.5 line-height
- **Card title** — 13px bold
- **Section heading (h3)** — 15px / 700 weight, navy color
- **View title (h1)** — 22-30px / 800 weight, navy color, -0.5px letter-spacing
- **Eyebrow** — 9-11px, letter-spacing 1-2px, uppercase, 700 weight, H-FARM red color
- **Meta / supporting** — 10-12px, ink-soft color
- **Tabular numerals** — used for scores, counts (font-variant-numeric: tabular-nums)

### Weight
- 400 (regular) — body text
- 500 (medium) — secondary labels
- 600 (semibold) — meta/sub-labels
- 700 (bold) — titles, prominent labels
- 800 (extra bold) — h1, KPI numbers, eyebrows

## Spacing

8px-based scale, with small accommodations for 4 and 6:
- **4px** — tight gaps inside chips
- **6px** — small icon-text gaps
- **8px** — base unit
- **10-12px** — input padding, card padding
- **14-18px** — card outer padding, section spacing
- **22-28px** — page-level padding, hero spacing

## Border radius

- **3-5px** — small chips, input fields, code blocks
- **6-8px** — cards, modals, KPI tiles
- **10-12px** — primary modals, login overlay
- **50%** — avatars, dots, FAB buttons

## Elevation / shadows

Minimal — flat with subtle lift on hover:
- **Card rest** — none, just border
- **Card hover** — `0 4px 12px rgba(10,37,64,.12)` (slight blue-tinted shadow)
- **Floating action button** — `0 4px 14px rgba(217,83,79,.4)` (red-tinted)
- **Modal** — `0 20px 60px rgba(0,0,0,.3-.4)`
- **Toast** — `0 6px 20px rgba(0,0,0,.22)`

## Component patterns

### Buttons
- **Primary** — H-FARM red bg, white text, 9px×18px padding, 5px radius, uppercase 12px 700 letter-spacing 0.5px
- **Secondary** — Ink bg, white text, same shape
- **Ghost** — transparent bg, rule border, ink-soft text, hover → navy border + navy text
- **Icon FAB** — circle, 42-52px diameter, fixed bottom-right

### Cards
- White bg on off-white page
- 1px rule border (warm rule color)
- 3px top-border accent (color per category — H-FARM red for default, priority color for entity cards)
- 6px radius
- Hover: translateY(-2px) + shadow
- Internal padding 12-14px

### KPI tiles (home view "Pipeline at a glance")
- Same as cards but with 3px TOP border colored by priority band
- Number 24-30px 800 weight
- Label 10-11px 700 letter-spacing uppercase
- Click → inline expand below (not page navigation)

### Pills / chips
- 9-10px font, 2-3px×8-12px padding, 9-11px radius (pill shape)
- Background = semantic color (priority/tier), white text
- Inline icon (★, ●) with 4-6px gap

### Inputs
- 1px rule border, 5-6px radius
- Paper background (not white) — signals "interactive surface, not display"
- Focus: gold/red border + white background
- Padding 7-10px×9-12px
- Search inputs: type="search" for native clear button

### Modals
- Backdrop: rgba(10, 37, 64, 0.5) — navy-tinted, not pure black
- Card: white, 10-12px radius, 24px+ padding, top-border accent in H-FARM red (4px) or gold for premium
- Title bar: navy bg, white text, 14-22px×22px padding, 3px gold bottom-border

### Toasts
- Bottom-right (sync toast: 80px from bottom, right 14px) or bottom-center (undo toast)
- Compact: 10×14-16px padding, 12px font, 6-8px radius
- Color by intent: ur green for success, hot red/amber for warnings, ink for neutral

### Sidebar nav
- Navy background, white text, 208px wide on desktop
- Section labels: 9px 1.8px letter-spacing uppercase gold
- Active item: H-FARM red soft tinted bg (10% red), gold text, gold left border (3px)
- Section divider gold (3px right-border on the sidebar itself)

## Voice & microcopy

- **Direct, specific, useful.** No hedging, no "I'd suggest", no
  "Based on the data".
- **Sentence case** for titles. NOT Title Case.
- **No emoji in titles or labels.** Reserve for tiny accents in toasts
  (✓, ⚠) and FAB icons (✨).
- **Numbers first, prose second.** "5 critical partners haven't been
  contacted in 30+ days" — never "There are some partners you might
  want to consider".
- **First person from the app sparingly.** Use direct address: "Pick a
  view from the left" — never "I'll help you pick a view".
- **Time-of-day greeting on home** — "Good morning, Defne Tuncer.",
  "Working late, Defne Tuncer." (uses identity from roster).

## Negative space

Generous. Cards have room around them. Sections separated by 22-28px
vertical space. Never crowd KPI tiles or chip rows.

## What this design system is NOT

- Not playful or whimsical (no rounded mascots, no bright illustrations)
- Not heavy / dense (no Bootstrap-style stacked panels)
- Not corporate-blue SaaS (no LinkedIn navy gradients)
- Not Material Design (no elevations 1/2/3/4/8 system, no FAB shadows
  that big)

If a generated mockup looks like Stripe, Notion, or the FT — good.
If it looks like Bootstrap, Salesforce, or a generic dashboard — wrong direction.

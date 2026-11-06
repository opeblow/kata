# Design system

Kata's interfaces (website and dashboard tab) follow one visual language. This document records the art directions we
considered, the one we chose, and the tokens that implement it.

## Art directions considered

**1. Sumi (chosen).** Paper, ink and a single vermilion seal. The name *kata* comes from practised forms; the identity
borrows the restraint of a printed manual. Warm off-white paper, near-black ink, one accent used only for the moment
of decision (approve) and for numbering. Serif display type for statements, a humanist sans for reading, a mono for
anything the agent executes. Ruled lines instead of cards; numbered sections. Motion is limited to a single stroke
drawing itself in the hero, which is the product in one gesture: many steps becoming one line.

**2. Instrument.** A lab-equipment aesthetic: light grey panels, oscilloscope green traces, dense monospace readouts.
Strong for an observability tool, but it reads as a monitoring product, and Kata is about a human decision more than a
live signal.

**3. Blueprint.** Kraft and cream with technical-drawing linework and ochre annotation. Expressive and memorable, but
the drawing language competes with the real data (steps, schemas) that users must read carefully before approving.

Sumi wins because approval is the core interaction: it needs calm, high-contrast reading surfaces and one unmistakable
signal for "this is the decision". It also avoids the generic AI-product defaults: no gradients, no glow, no glass, no
generic dark dashboard grid.

## Tokens

| Token | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#f4f0e6` | `#14130f` | Page background |
| `--paper-2` | `#ece6d8` | `#1c1a15` | Raised surfaces, diagrams |
| `--ink` | `#1a1814` | `#ede7da` | Primary text, primary button |
| `--ink-2` | `#4a453c` | `#c9c2b3` | Body copy |
| `--muted` | `#7a7366` | `#948c7d` | Labels, captions |
| `--rule` | `#d8d0bf` | `#2f2b24` | Hairlines |
| `--seal` | `#c8361d` | `#e04a2f` | The single accent: approve, section numbers |
| `--moss` | `#3f7a52` | `#6fae84` | Passed checks, read-only effects |

The dashboard tab inherits the host theme's text and background and derives its hairlines and fills from
`currentColor`, so it reads correctly on any Hermes dashboard theme while keeping the seal accent.

## Typography

| Role | Family | Notes |
|---|---|---|
| Display | Newsreader 500 | Headlines, stat values. Italic for the one emphasised phrase. |
| Text | IBM Plex Sans 400/500/600 | Body 17px / 1.6 on the site, 14px in the dashboard. |
| Code and data | IBM Plex Mono 400/500 | Tool names, arguments, eyebrows, section numbers. |

Scale: 12, 13, 14, 16, 17, 18, 20, 22, 24, 30, 48, 72. Headlines use `clamp()` and tight tracking (-0.015em).

## Spacing, layout and shape

- Spacing scale: 4, 8, 12, 16, 24, 32, 48, 72, 112.
- Content width 1180px with a 24px gutter (16px under 720px).
- Section layout: a 220px label column (number and name) beside the content.
- Radius: 6px for controls, 10px for figures. No shadows; depth comes from rules and surface tone.
- Breakpoints: 980px (single column), 720px (phone).

## Components

- **Buttons.** Ink (primary), line (secondary). On the dashboard the approve action is the only seal-filled button.
- **Tags.** Pill outlines for status and effect: moss for read only, seal for external or needing attention, muted
  otherwise. Colour is always paired with a word.
- **Steps.** Numbered circles joined by a hairline, tool name in mono, arguments in a tinted code block, the effect
  reason underneath.
- **Checks.** Mark, name, detail in a three-column grid; failing checks switch to the seal colour.

## Motion

- One hero stroke drawing in over 1.4s; section reveals of 10px over 600ms.
- Transitions of 120 to 160ms on hover and focus colours only.
- Everything is disabled under `prefers-reduced-motion`.

## Accessibility

- Visible focus rings in the seal colour on every interactive element.
- Status is never conveyed by colour alone.
- Skip link, landmark regions, and `aria-live` on the dashboard detail pane and notices.

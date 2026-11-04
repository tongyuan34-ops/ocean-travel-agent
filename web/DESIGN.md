# Ocean Web Design

## North Star

Make trip planning feel like a calm, capable conversation at the water's edge. Keep the ocean photography and editorial headline as the brand moment; make the working assistant crisp, legible, and dependable.

## Product register

Hybrid. The landing view carries the travel brand. The assistant, session rail, permission prompts, and generated media use a focused product interface. Real tool status and safety decisions remain visually distinct and never become decorative.

## Palette

- Deep current `#17354A` — primary actions and conversation anchors.
- Coastal ink `#142B3B` — primary text.
- Sea glass `#397F79` — focus, active, and connected states.
- Pale foam `#E8F2EF` — selected and positive surfaces.
- Warm sand `#EFC48B` — neutral activity and travel warmth.
- Cloud white `#FAFCF9` — assistant surfaces.

Runtime ownership: the existing CSS custom properties and component rules in `styles.css` are canonical. `--ink`, `--navy`, `--sea`, `--sea-soft`, `--sand`, and `--paper` map the palette to the rendered interface. This document records their intent; it does not generate CSS.

## Typography

- Display and editorial headings: the system serif stack beginning with Iowan Old Style and Baskerville, falling back to Chinese serif and Georgia fonts.
- Interface and body: the existing system sans stack with platform Chinese fonts.
- Metadata and technical values: the existing system monospace stack; use tabular numerals for comparable values.
- Use restrained uppercase tracking for short English labels only. Keep Chinese copy at comfortable body sizes and line heights.

## Layout and surfaces

- Preserve the full-viewport photographic landing page and the two-column assistant with a collapsible session rail on small screens.
- Use bright, softly tinted assistant panels over the dark ocean veil. Surfaces should separate by lightness, spacing, and subtle borders rather than hard outlines.
- Keep conversation content spacious and readable. Tool details and status stay inline with the assistant response.
- Use rounded rectangular controls for actions and cards; reserve pill shapes for metadata and secondary chips.

## Interaction and accessibility

- All controls retain their existing handlers and state. Style hover, active, disabled, and keyboard focus states without introducing new behavior.
- Sea-glass focus rings remain visible. Reduced-motion preferences suppress hover lifts and existing animation is already reduced by the global rule.
- Keep permission, error, warning, success, loading, and empty states legible as distinct states.
- Maintain the existing safe-area and narrow-screen behavior.

## Signature

The sea-to-assistant transition: full-bleed destination photography gives way to a clear, bright planning workspace, linked by a consistent deep-current and sea-glass palette.

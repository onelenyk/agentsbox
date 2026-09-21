# Design system

The whole web UI is styled from **tokens** in `web/static/app.css`. The pages
are built from **components** (CSS classes, and Jinja macros in
`web/templates/_ui.html` where markup repeats). A restyle means changing token
values. It never means editing templates or hunting down one-off rules.

`tests/test_design_system.py` enforces all of this. If one of those tests fails,
the change it caught is a place a future restyle would have to find by hand.

## Restyling

| To change…                        | edit                                             |
|-----------------------------------|--------------------------------------------------|
| colours, borders, shadows         | colour tokens in `:root` (`--bg`, `--panel`, `--text`, `--accent`, `--good`…) |
| the type scale                    | `--fs-*`, `--fw-*`, `--lh-*`, `--ls-*`           |
| density (all spacing)             | `--sp-1` … `--sp-13`                             |
| corner shape                      | `--r-xs`, `--r-sm`, `--r-md`, `--r`, `--r-lg`, `--r-pill` |
| fonts                             | `--sans`, `--mono`                               |
| motion                            | `--dur-fast`, `--dur`, `--dur-slow`, `--dur-ambient` |
| focus rings, stacking             | `--ring`, `--ring-good`, `--z-*`                 |

### Adding a theme

A theme is a `:root[data-theme="<name>"] { … }` block that sets token values
and nothing else. The light theme in `app.css` is the working example: copy it,
change the values, and set `data-theme="<name>"` on `<html>`. The switch in the
sidebar footer stores the choice per browser.

A theme must retune **every** colour token. Any it skips keep their dark value,
and the tests fail. A theme may not style selectors or invent tokens.

Two things can't read a `var()`, so they carry their colour inside the token:
- `--icon-search` is a data-URI SVG. Retune its stroke with `--muted`.
- Mermaid diagrams read their colours from the tokens when they initialise
  (`plans.html`).

## Rules for new UI

1. **Colours only from tokens.** No hex, `rgb()` or `hsl()` outside the token
   blocks, in CSS or in templates. For a new meaning, add a semantic token such
   as `--info` and give every theme a value for it.
2. **Sizes only from the scales.** Every `font-size`, `font-weight`,
   `line-height`, `letter-spacing`, spacing value (`padding`/`margin`/`gap`),
   `border-radius`, duration and `z-index` is a `var(--…)`. Literals are
   allowed for structure only: `0`, a `1px` hairline, `50%` circles, and layout
   widths and heights.
3. **No `style=""` that styles.** A `style` attribute may only pass a
   component a value the server computes, as a custom property:
   `style="--w: 40%"`, read by the CSS as `width: var(--w, 0%)`.
4. **No `<style>` blocks in templates**, and no `element.style.x = …` in
   scripts. Show and hide with `hidden` or a class.
   `style.setProperty('--x', …)` is fine.
5. **Repeated markup is a macro** in `_ui.html`, for example `toolbar`,
   `dsec`, `fact`, `file_row`, `choice`, `probe` or `model_picker`.
6. **Name new classes after their component** (BEM: `.block__part--variant`).
   Grep `app.css` first: `.row`, `.ask`, `.tag` and `.kv` have all collided
   before.

## Shared building blocks

- **Tones:** `.tone-good`, `.tone-bad`, `.tone-warn`, `.tone-muted` for
  coloured text.
- **Status:** `.badge` (`--ok`, `--warn`, `--accent`, `--mono`), `.health`
  with its dot, `.probe` (`--ok`, `--bad`).
- **Layout:** `.panel`, `.ecard`, `.card-grid`, `.cols-2`, `.field`,
  `.field-pair`, `.choices`, `.modal` (`--wide`, `--tall`), `.toolbar`,
  `.dsec`.
- **Data:** `.bars` / `.bar-row` / `.bar-fill` (width through `--w`, colour
  through `.bar-fill--tone` plus `--c`), `.spark` (height through `--h`),
  `.statline`, `.meta-line`.

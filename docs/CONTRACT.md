# Page contract: what the model produces, what the template does

The model never writes HTML pages. It returns two things, and `src/p2p/assemble.py`
combines them with the fixed template (`src/p2p/template/`) into one offline `index.html`.

## 1. `compute_js` — the only calculation code

```js
function compute(state) {        // pure: no DOM, no network, no randomness, no globals
  return {
    outputs:       { key: number | number[] | number[][] | string | boolean, ... },
    intermediates: [ { label, value, note?, decimals? } ],   // shown as step-by-step values
    checks:        [ { label, pass: boolean, detail? } ]     // shown as live self-checks
  };
}
```
`state` holds one entry per control id. Every number on the page comes from here.
It runs in strict mode inside a wrapper (`assemble.wrap_compute`); the numeric checks in
Stage 6 run the exact same wrapper in QuickJS.

## 2. `spec` — text, controls and visuals (JSON)

| Field | Content |
|---|---|
| `title`, `subtitle` | page heading and one-line summary |
| `meta` | `paper_title`, `section_label`, `equation_label`, `source_url` (set from case.json) |
| `sections.idea` | `what`, `why` |
| `sections.equation` | the equation in MathML (or HTML-lite) |
| `sections.symbols[]` | `symbol`, `meaning`, `shape` |
| `sections.explorations[2]` | `title`, `change`, `observe`, `why`, `preset` {control id: value} |
| `sections.limitation` | `kind` (limitation, assumption or misconception), `text` |
| `controls[]` | see below |
| `outputs[]` | headline numbers: `key`, `label`, `decimals`, `unit` |
| `visuals[]` | see below |
| `grounding` | `from_excerpt[]` (verbatim quotes), `our_simplifications[]` |

Display strings may use: `b i em strong sub sup code br span small p ul ol li` and MathML
Core (`math mi mn mo mrow msub msup msubsup mfrac msqrt mroot mover munder mtext mtable mtr
mtd …`). Everything else is stripped by the sanitizer.

### Controls
Common: `id`, `type`, `label`, `help`, `default`, `min`, `max`, `step`.

| type | value in state | extras |
|---|---|---|
| `slider`, `number` | number | `unit` |
| `toggle` | boolean | |
| `select` | option value | `options`: strings or `{value, label}` |
| `vector` | number[] | `labels`, `length_from` (control id), `normalize`, `show_sum`, `resizable` [min, max], `fill` |
| `matrix` | number[][] | `row_labels`, `col_labels`, `rows_from`, `cols_from`, `resizable` {rows:[min,max], cols:[min,max]}, `fill` |

`length_from` / `rows_from` / `cols_from` tie a size to a number control (e.g. "number of
outcomes" resizes the probability vector).

### Visuals
Values are references such as `"outputs.weights"` or `"state.p"`.

| type | fields |
|---|---|
| `bar` | `values` or `series[{values,label}]`, `labels`, `highlight: "max"`, `x_label`, `y_label`, `y_min`, `y_max`, `decimals` |
| `line` | `series[{x, y, label, dashed}]` (or shared `x`), `points[{x, y, label}]`, axis labels and ranges |
| `heatmap` | `values` (2-D), `row_labels`, `col_labels`, `decimals`, `scale` (sequential or diverging), `show_row_sums` |
| `matrix` | `values` (2-D table), labels, `decimals` |
| `svg` | `width`, `height`, `items[]` of `box`, `circle`, `arrow`, `line`, `text`; any number may be a reference; labels may contain `{outputs.key:2}` |

All visuals take `title` and `caption`, and `caption` may contain `{outputs.key}`.

## What the template guarantees
Fixed section order and ids: `#idea #symbols #playground #explore-1 #explore-2 #limitation
#grounding`. Accessible inputs, keyboard support, reset button, preset buttons, an error
box instead of a blank page, values that changed are highlighted, a fixed disclaimer that
the demo does not reproduce the paper's results, no network access, system fonts only.

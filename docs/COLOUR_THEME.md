# Colourful theme (1.4.0) - presentation only

The PPC Control Center, the machine popup, the detail panel, the dialogs, the PPC Control Center workspace and the PPC Settings form share
one colour language. **Nothing functional changed**: no Python logic, API, DocType, custom field, migration, route, field name, payload,
filter, calculation or event handler was touched. Every control keeps its original handler; the only JavaScript changes add markup that is
purely visual (icons, CSS classes).

## Colour language

| Meaning | Colour | Used for |
|---|---|---|
| Navigation, headings | deep navy `#0b1b3f` → `#14306b` | tab strip, side panel and dialog headers, page titles |
| Primary action | indigo `#4f46e5` → blue `#2563eb` | primary buttons, focus rings, selected row/tab, links |
| Available / good / progress | green `#15803d`, teal `#0f766e` | available machines, work in progress, completed, utilisation |
| Warning / upcoming | amber `#b45309`, orange `#c2410c` | upcoming availability (next free slot of a busy machine), "not configured", conflicts KPI |
| Conflict / failure / overdue | red `#c0262d` | busy machines, overdue, failed synchronisation, the Today marker |
| Inactive / incompatible | slate `#475569` | wrong-type machines, drafts, completed bars |

Every colour has a dark-theme variant (lighter, so text on a tinted surface keeps at least 4.5:1 contrast; this is checked by the browser test).
Gradients are subtle, cards are rounded (12 px) with soft shadows, text is the Frappe font.

## Screens

* **Dashboard** - the ten KPI cards have a colour-coded icon tile, a large value, a title and a tinted accent: plans indigo, pending quantity blue,
  drafts slate, open Work Orders sky, Job Cards violet, work in progress green, completed teal, overdue red, conflicts orange, failed synchronisations red.
  Values, calculations, click targets and data sources are unchanged. Each dashboard panel has its own accent colour.
* **Lists** (Production Plans, Work Orders, Job Cards, Schedule History, Exceptions) - tinted table headers, zebra rows, indigo hover, pill badges per status, rounded filter card, pill chips.
* **Production Gantt** - gradient bars per existing status (open blue, material transferred teal, in progress green stripes, on hold amber, done slate), a
  violet dashed baseline, a green gradient actual-execution line, indigo dependency arrows (red when violated), a red Today line with a label tab,
  tinted weekend columns, indigo group headers, selected-row highlight, rounded tooltips, thin indigo scrollbars.
* **Machine selection** - each machine is a coloured row from the status the server returned: **green** available, **red** busy, **indigo** current,
  **gray** wrong type (incompatible), with pill badges and an **amber** "next free slot" for busy machines. The rows are the same data in the same order; no rule changed.
* **Dialogs and the detail panel** - navy header (red for conflicts, orange for partial failures, green for successes), tinted footer, gradient primary button, key/value blocks.
* **PPC Control Center workspace** and **PPC Settings** - coloured shortcut cards / sections (scoped to those two routes).

## Where the styling lives

| File | What |
|---|---|
| `public/css/ppc.bundle.css` | the whole PPC stylesheet; the new section "COLOUR THEME (1.4)" is appended at the end (override layer, every selector starts with `.ppc`) |
| `public/css/ppc_theme.bundle.css` (new) | the workspace and the settings form only; every selector starts with `body[data-route=...]` |
| `hooks.py` | one line registers `ppc_theme.bundle.css` (`app_include_css`) |
| `public/js/ppc_shell.js` | inline SVG icons (no dependency) for the navigation and the KPI cards |
| `public/js/ppc_common.js`, `ppc_machine.js`, `ppc_widgets.js`, `ppc_pages.js` | a `ppc.skin(dialog)` call (adds a CSS class) and status CSS classes on machine rows |

Removing the "COLOUR THEME (1.4)" block and the new file restores the previous look.

## Not changed on purpose

Rows that look "available" in the machine popup are exactly those the server marks available. A machine whose Workstation `status` is Off,
Problem or Maintenance still shows as available when it has free capacity (the status is shown next to its name); making such a machine unavailable would be a
scheduling rule, not styling, so it is not part of this change.

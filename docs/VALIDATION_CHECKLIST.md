# Validation checklist

Legend: ✅ verified by an automated test run · 🖥 verified once by hand in a real browser (read-only smoke) ·
⚠ **not verified — must be tested on your real site** · ➖ handled by the standard ERPNext Production Plan, not by PPC.

Tested against: ERPNext 15.108.3, Frappe 15.108.0, MariaDB, Python 3.11, on a development copy (`dev.local`) that also runs
`india_compliance`, `hrms` and several customer apps (including `chemical_mfg`, which overrides Work Order / Job Card).
**Nothing was installed on or tested against Frappe Cloud.**

## Design change you should know about

An earlier build had its own "PPC Production Plan" DocType (Sales Order fetch, quantity maths, BOM checks, Work Order creation).
It was **removed**: PPC now works on the standard Production Plan, so those parts are ERPNext's own and are not re-tested here.
PPC adds automatic draft Work Orders on submit, the connections/buttons/panel, and the control screen + Gantt.

## Automated tests

| Suite | Tests | Covers |
|---|--:|---|
| `test_planning` | 15 | it is the standard plan; custom fields + Connections override installed; **draft Work Orders created automatically on submit** (BOM, warehouses, operations); option off → nothing created; the submit is only *queued*; **re-running never duplicates**; the standard button cannot create a second Work Order; guard off for non-PPC plans; a hand-deleted row is recreated exactly once; **a failing row doesn't stop the others, is audited, and Retry fixes only it**; audit trail; auto-submit + Job Cards + baseline; no cards without operations; plan cancel removes drafts; cancelled plan refused; pending flag |
| `test_work_orders` | 7 | submit through the normal lifecycle (+Job Cards); **submit failure reported not bypassed, then retried**; **bulk submit** (drafts of the plan only, never resubmits, foreign/unknown skipped); one failure doesn't stop the rest; cancelled never submitted; cancel/delete not blocked by PPC; auto-submit |
| `test_job_cards` | 12 | cards follow BOM operations, tagged, baseline recorded; none without operations; draft WO; **duplicate prevention**; only the missing operation regenerated; re-tag/re-baseline; cancelled card left alone; **bulk submit** (predecessors first, per-record results, never twice, foreign/unknown skipped); ERPNext failures reported without stale alerts; one failure doesn't stop others; cancelled; batch limit |
| `test_scheduling_pure` | 12 | concurrency sweep, sequence groups, cascade (gap/duration/parallel/blocked), sequential layout |
| `test_scheduling` | 22 | **reschedule move/resize**, **Work Order date sync**, original-vs-revised, audit trail, actual dates untouched, **invalid dates**, **dependency violations**, cascade, **workstation conflicts/capacity**, protection of started/completed/cancelled cards, locked Work Order, stale write, holiday/delivery/past warnings, **midnight/month-end/leap-day/year boundaries**, whole-day shift, browser strings, auto-schedule |
| `test_queries` | 16 | every filter, PPC-only scope, dates, overdue/unassigned/conflict, cancelled excluded, **pagination of 120 cards**, sort whitelist, Gantt cap/truncation/filtering/dependencies/locked/estimated, 14 KPIs + filter response, workload, Work Order list, audit endpoint |
| `test_api_security` | 11 | viewer cannot mutate even with ERPNext write rights, no-PPC-role denied, ERPNext permissions still enforced, supervisor/planner/manager differences, override capability, settings remap, foreign Work Order untouched, **SQL-injection inertness**, error masking, POST-only/no-guest endpoints |
| **Python total** | **95** | all passed on the last full run (end of this file) |
| `tests/js` (Node) | 27 | Gantt maths in 5 time zones (UTC, Berlin, New York, Kolkata, Auckland) + DOM: render, scales, drag, resize, cancel, reject→revert, locked, read-only, grouping, search, tooltip escaping, server-clock today line |

How the 28 areas you listed map: 1–6 plan creation / Sales Order & finished-goods fetch / BOM validation / 8 over-planning → ➖ standard Production
Plan · 7 duplicate Work Orders ✅ · 9–10 Work Order create/submit ✅ · 11–14 operations / Job Cards / duplicates ✅ · 15–16 bulk submit ✅ ·
17 permissions ✅ · 18 Gantt filtering ✅ · 19–20 reschedule + Work Order sync ✅ · 21 invalid dates ✅ · 22 dependencies ✅ · 23 conflicts ✅ ·
24 completed protection ✅ · 25 partial failure/retry ✅ · 26 cancelled documents ✅ · 27 large lists ✅ · 28 time zone/date boundary ✅ (server and Gantt maths).

## Browser smoke (🖥, Edge, development server, read-only)

Logged in as a temporary System Manager user (removed afterwards), against the real submitted plan `MFG-PP-2026-00002`
(253 draft Work Orders created by another user): the **standard Production Plan form** shows the *Production Control* button group
(Create Draft Work Orders · Retry Failed · Refresh Job Cards · Generate Missing Job Cards · Schedule Unscheduled Operations · Open Gantt · Open Production Control),
the *Connections* panel shows the new **Production Control** group (Job Card, PPC Audit Log), and the embedded panel shows 14 KPIs and
the 253 Work Orders paginated (1–50 of 253). The control page filtered to that plan shows its header strip and KPIs; the Gantt tab renders (no Job Cards
exist yet); a *new* Production Plan shows the PPC fields. **No JavaScript errors.** No create/submit button was clicked.

Side observation: another user of the same dev site used the standard plan to create those 253 Work Orders while I was testing;
the control screen and plan overview reflected them immediately.

## ⚠ Must be verified on your real site

**Front end with live actions** (not exercised in a browser — covered by API tests and jsdom only):
- [ ] Submitting a plan with *Auto Create Draft Work Orders on Submit* ticked → drafts appear (needs a background worker); *Retry Failed* after a forced failure.
- [ ] **Submit Selected Work Orders / Job Cards** dialogs: confirm, progress bar, per-record result; *Select all matching* with more than one page.
- [ ] **Gantt on real Job Cards**: drag a bar, drag an edge, the confirmation dialog (warnings, shift-following, override, reason), double-click date dialog,
      tooltip, grouping, dependency arrows, original-vs-revised marker, today line, locked 🔒 bars, dark theme.
- [ ] Real-time progress after background creation (needs socket.io). Narrow/mobile layout (task list hides below 768 px by design).

**Environment.**
- [ ] Install + `bench build` + migrate on **Frappe Cloud** (staging first); apps-screen entry; roles appear.
- [ ] Background workers on your plan run the `ppc_create_wo::…` / `ppc_refresh::…` jobs; hourly job `refresh_active_plans`.
- [ ] `before_uninstall` removal of the custom fields (deliberately **not** executed on the dev site — it drops columns).
- [ ] Patches `remove_ppc_plan_doctypes` and `retag_job_cards` through a real `bench migrate` (they were run directly on the dev site, not via migrate).

**Business rules of your site.** Serial/batch finished goods, variants, product bundles, subcontract rows, *Consolidate Items* plans
(their Work Orders carry no Sales Order → Sales Order/Customer filters cannot match), workstations with working hours, long holiday lists,
thousands of open Job Cards (performance), approval workflows on Work Order / Job Card, Server Scripts that reject submit
(PPC reports their message — check the wording you want).
- [ ] Run one real Sales Order end to end (plan → auto drafts → submit → Job Cards → reschedule → submit cards) and compare with the usual way.
- [ ] Large plans (≥ 50 rows) and a 500-bar Gantt at realistic volumes. [ ] The capability table matches your organisation.

## Manual test script (15 minutes, staging)

1. Pick/create a Sales Order with two items that have default BOMs (one with operations) and a delivery warehouse. Ensure Manufacturing Settings has default WIP/FG warehouses.
2. **Production Plan ▸ New** → *Get Items From: Sales Order* → add the order → **Get Finished Goods for Manufacture**. Leave *Auto Create Draft Work Orders on Submit* ticked (section *PPC Production Control*). **Submit**.
3. After a few seconds open *Connections ▸ Work Order*: drafts exist, one per row. Click **Production Control ▸ Create Draft Work Orders** again: *0 created, N already existed*.
4. Click the standard **Create ▸ Work Order / Subcontract PO**: it must refuse with "…already exists for this Production Plan item".
5. **Production Control ▸ Open Production Control** → *Work Orders* tab → tick → **Submit Selected Work Orders**. Job Cards appear under *Connections ▸ Production Control ▸ Job Card*.
6. *Gantt* tab: drag an operation later than its successor → warning → tick *Shift following operations* → Save. Check the *Audit Log* tab and the Work Order dates.
7. Break something on purpose (e.g. clear the WIP warehouse default, submit) → failure listed with ERPNext's message → fix → **Retry Failed**.
8. Log in as a `PPC Viewer` user: everything visible, nothing can be changed.

# PPC Control Center (1.2.0) — UI redesign

1.2.0 replaces the single tabbed "PPC Production Control" screen with a **PPC Control Center**: one Workspace and eight
separate pages. ERPNext stays the source of truth, the standard Production Plan stays the planning document, and every
rule of 1.1 (baselines, append-only revision history, Work Order sync, failed-sync recovery, permissions) is unchanged.

## Where things are

| Page | Route | Source of its data |
|---|---|---|
| Workspace **PPC Control Center** | `/app/ppc-control-center` | links to everything below, PPC Settings, PPC Audit Log, Production Plan |
| Dashboard | `/app/ppc-dashboard` | `get_dashboard_overview` — one aggregated call (10 KPIs + 6 sections) |
| Production Plans | `/app/ppc-production-plans` | `get_production_plans` (standard Production Plan + counts) |
| Work Orders | `/app/ppc-work-orders` | `get_work_orders`, `precheck_work_orders`, `submit_selected_work_orders` |
| Job Cards | `/app/ppc-job-cards` | `get_job_cards`, `submit_selected_job_cards`, `get_schedule_history` |
| Production Gantt | `/app/ppc-gantt` | `get_gantt_data` (filters + date window), `reschedule_job_card` |
| Workload & Capacity | `/app/ppc-workload` | `get_workload` |
| Schedule History | `/app/ppc-schedule-history` | `get_revision_log` (PPC Schedule Revision, read-only) |
| Exceptions | `/app/ppc-exceptions` | `get_exceptions`, `repair_schedule_inconsistencies`, `initialize_missing_baselines`, `retry_failed_synchronizations` |
| PPC Settings | `/app/ppc-settings` | standard form |

Page names (`ppc-…`) never equal the Workspace name (`ppc-control-center`), so there is no route collision.
Every page accepts filters in the URL, e.g. `/app/ppc-gantt?plan=MFG-PP-2026-00005`, and every KPI, panel link and plan
link uses the same mechanism. The old `/app/ppc-production-control?tab=…&plan=…` route still works and forwards to the
matching new page.

## What was retained, moved, redesigned, removed

**Retained unchanged** — the whole backend of 1.1 (planning, auto draft Work Orders, duplicate prevention, retries, Job
Card baselines, reschedule engine, cascade, dependency/conflict validation, Work Order sync, reconciliation, audit log,
capability roles), the Gantt component (`public/gantt/ppc_gantt.js`: bars, drag/resize, ghost baseline bar, actual line,
revision badges, schedule columns, dependency arrows), `chemical_mfg` integration (`doc_events`), fixtures and patches.

**Moved** — the Gantt from a tab inside the control screen to its own full-width page; Work Order / Job Card tables to
their own pages; the consistency check, baseline initialization and retry buttons to the Exceptions page; the audit
listing to Schedule History (the *PPC Audit Log* DocType list stays one click away in the workspace).

**Redesigned** — filters (one toolbar per page, primary filters plus "More filters", debounced search, "Clear filters");
KPIs (14 → 10, each opens the filtered list and equals its total); tables (server-side pagination 25–200, sortable columns,
column chooser, sticky header, row selection, "select all matching the filters"); bulk submit (a pre-check dialog shows
every problem *before* anything is submitted); the schedule detail panel (a right-hand drawer, keyboard- and
screen-reader-friendly); the Production Plan form (a one-line summary with links instead of an embedded dashboard and Gantt).

**Removed** — the in-form dashboard/Gantt, the tab bar, the 14-tile KPI strip, and the combined "control panel" filter grid.
Nothing a user could do before is gone; each action has a page (see the table above).

## Behaviour worth knowing

* **Dashboard date range.** Plans by posting date; Work Orders and Job Cards by *schedule overlap* (an operation is
  included when any part of its current schedule falls in the range). The same meaning is carried to every KPI drill-down.
* **KPI numbers equal the list totals** (verified by the browser test for nine of the ten KPIs; the tenth, Pending
  Production Quantity, is a sum and opens the plan list).
* **Work Order pre-check.** `precheck_work_orders` runs ERPNext's own `before_save` and validation methods inside a
  savepoint that is always rolled back, plus PPC's checks (draft, permission, plan, mandatory data). It lists problems per
  Work Order; "Submit" then goes through `doc.submit()` exactly as before. Rules enforced only at the moment of submission
  are still reported per record afterwards. Nothing is bypassed.
* **Job Card bulk submit.** Select rows, or "Select all draft Job Cards matching the filters" (server-side id list, all
  pages); a confirmation dialog; earlier operations first; one result per record.
* **Gantt.** Fetches only the filtered window (default: 7 days back to 45 days ahead; "Show all dates" removes it); at most
  `max_gantt_tasks` bars (default 500) with a clear notice when truncated. A change is validated and saved by the server; if it
  is refused or the card was changed by someone else, the bar returns to its previous position with the reason.
* **Workload.** Available hours come from the Workstation's working hours and holiday list × production capacity. A
  workstation without working hours shows **unavailable**, with no utilization figure — nothing is estimated and no
  schedule is ever changed from this page.
* **Exceptions.** Failed synchronization, schedule drift, Work Order out of step, workstation conflict, dependency conflict,
  overdue operation, missing/invalid Work Order link, revision-history mismatch, missing baseline, missing history. Repairs
  are previewed first; started or finished work is never repaired automatically.
* **Single request per page load.** The filter toolbar waits for Frappe's (asynchronous) controls to take their values
  before a page reads them, and page scripts capture route options while Frappe still holds them.

## Upgrade notes

* `bench migrate` registers the eight Pages and the Workspace and runs `patches/v1_2/remove_old_workspace.py`, which removes
  the old *PPC* workspace **only** when *PPC Control Center* exists and the old one belongs to this app's module and is public.
  No document data is touched; there is no schema change in 1.2.0.
* `bench build --app ppc_production_control` (Frappe Cloud does it on deploy) and a browser hard-reload pick up the new bundle.
* Roll back by redeploying the 1.1.0 commit; the old workspace comes back with it, and the new Page records are harmless.

## Limitations

* The pre-check cannot predict a rule that ERPNext raises only inside `submit()` (for example stock availability checks
  that depend on other documents submitted at the same moment). Those are listed per record after the submit.
* Workload considers working hours × capacity; breaks, shift overrides and per-day maintenance are not modelled.
* The Gantt window and `max_gantt_tasks` bound the data fetched; very large sites should filter by plan or workstation.
* Dark theme follows Frappe's theme attribute; PPC colours are defined as variables and were measured for contrast
  (≥ 4.5:1 for text), but custom desk themes are not tested.
* Browser tests were run with Edge (Chromium) only.

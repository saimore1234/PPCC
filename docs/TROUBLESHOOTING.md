# Troubleshooting

Start with **PPC Audit Log** (`/app/ppc-audit-log`, also under the plan's *Connections ▸ Production Control*). PPC records the real
reason for every failure; the screen shows it in plain text.

## Work Orders

| Symptom | Cause / fix |
|---|---|
| No Work Orders appear after submitting the plan | *Auto Create Draft Work Orders on Submit* was off (older plans have no value), or no background worker is running. Use **Production Control ▸ Create Draft Work Orders** (runs inline for ≤10 rows, otherwise in the background). Check `bench worker`. |
| Some rows were not created | Open the Audit Log: each failed row has its reason and *Retry Status = Pending*. Fix the cause → **Production Control ▸ Retry Failed**. Only missing rows are processed. |
| "Work Order … already exists for this Production Plan item" | The duplicate guard. The plan uses auto-creation and that row already has a live Work Order — find it under *Connections ▸ Work Order*. (The standard *Work Order / Subcontract PO* button can still create subcontract POs.) |
| "Work-in-Progress Warehouse is required before Submit" | Set it in Manufacturing Settings (default) or on the Work Order, then submit again. PPC never bypasses it. |
| Work Order submit blocked by a custom rule (e.g. insufficient stock) | That is your site's own Server Script/validation. PPC reports its message, leaves the Work Order a draft and logs a failed *Submit Work Order* entry; **Retry Failed** resubmits once fixed. |
| I deleted a draft Work Order and want it back | **Create Draft Work Orders**: the missing row gets exactly one new Work Order. |
| Cannot cancel a plan | Standard ERPNext: cancel its submitted Work Orders first; draft ones are removed automatically. |
| Sales Order / Customer filters find nothing for a plan | The plan uses *Consolidate Items*, so its Work Orders carry no Sales Order. Filter by Plan, Item or Work Order instead. |

## Job Cards

| Symptom | Cause / fix |
|---|---|
| No Job Cards | The BOM has no operations (by design nothing is created), or the Work Order is still Draft. Submit it; ERPNext creates the cards. |
| Job Cards missing from the PPC list | Run **Refresh Job Cards** (tags cards of the plan's submitted Work Orders; idempotent). Cards of Work Orders without a Production Plan are hidden unless *Include Job Cards without a Production Plan* is ticked. |
| Cards show `~` / "Estimated" dates | ERPNext held no schedule (capacity planning disabled). Run **Schedule Unscheduled Operations** or leave them as display-only estimates. |
| Bulk Job Card submit reports "Time logs are required…" / "…must be completed before…" | ERPNext's own rules. Record time/finish the earlier operation first. PPC submits earlier operations first within a selection. |
| "Qty To Manufacture in the job card cannot be greater…" | ERPNext's over-production guard on Job Card quantity (Manufacturing Settings → Overproduction %). |

## Gantt / rescheduling

| Symptom | Cause / fix |
|---|---|
| Bar will not move (🔒) | Started, completed, submitted or cancelled card, or Stopped/Closed/Completed Work Order, or you lack the *Reschedule* capability. Hover for the reason. |
| "Starts before the preceding operation ends" | Sequence rule. Choose later dates, or move the earlier operation. Only *Override Conflicts* holders may save it. |
| "Ends after the next operation starts" | Tick *Shift following operations* in the dialog, or shorten. |
| "Workstation … already booked" | Capacity exceeded on that workstation; pick another slot or raise Workstation *Production Capacity*. |
| "was changed by someone else. Refresh" | The card changed since your screen loaded (optimistic concurrency token). Nothing was saved and no revision exists; the screen reloads itself, then repeat the move. |
| Bar shows `⚠` / Sync status **Failed** | The last reschedule was rolled back (nothing changed, no revision). The audit log keeps the error and the proposal: **Retry Failed Synchronizations** replays it with re-validation. |
| Sync status **Drift** | The Job Card's ERPNext schedule no longer equals the last schedule PPC accepted (manual edit, data import, another app). **Check Schedule Consistency** → preview → repair re-applies the accepted schedule as a new revision (movable cards only). |
| "Original" shows `–` / "Initialized from existing schedule" | The card had a schedule before baselines existed. **Initialize Missing Baselines** (preview first) records its current schedule, labelled as such — the true original cannot be known. |
| Revision history looks wrong / `revision mismatch` | Duplicated or missing revision numbers are never rewritten automatically (reported only). History rows cannot be edited or deleted; a System Manager may add an audited correction note. |
| Work Order start moved after the first operation finished | By design the Work Order window is the earliest start/latest end of its *remaining* Job Cards, so the next reschedule moves the start to operation 2. Until then a Work Order that still spans the finished operation is **not** reported as inconsistent. |
| Work Order dates do not follow | They follow the *remaining* cards' earliest start/latest end after each reschedule; completed cards are ignored. Use the *sync* API if needed: `sync_work_order_schedule`. |
| Gantt shows "list truncated" | More than the configured bars match; narrow the filters (or raise *Max Gantt Bars* in PPC Settings). |
| Today line off by hours | It follows the **site** clock (system time zone), not your browser's. |
| No live progress bar | Needs the socket.io service. The page still works; reload to see results. |

## Permissions

| Symptom | Fix |
|---|---|
| "You are not permitted to perform this action (…)" | You lack the PPC capability. See PPC Settings → Capability to Role Mapping. |
| "No read/write permission on Job Card/Work Order…" | You lack the ERPNext permission. Add `Manufacturing User/Manager` (and `Sales User` to fetch Sales Orders). PPC roles never widen ERPNext access. |
| The app is not on the apps screen | Only users with a PPC role (or System Manager) see it. |

## Installation / build

| Symptom | Fix |
|---|---|
| `/app/ppc-production-control` opens a blank page / old UI | `bench build --app ppc_production_control` then `bench --site <site> clear-cache`; hard-reload the browser. |
| Install fails on `Duplicate entry 'PPC Production Control'` | A previous install aborted after creating the Module Def. Re-run `install-app … --force`. |
| Custom fields missing | `bench --site <site> migrate` (re-creates them) or `bench --site <site> execute ppc_production_control.install.ensure_custom_fields`. |
| Upgrading from the prototype with its own plan DocType | `bench migrate` runs `remove_ppc_plan_doctypes`, which deletes those DocTypes/tables/columns only when empty (otherwise it keeps them and logs an Error Log entry). |
| `Cannot connect to redis_cache` during build | Harmless for the build; start Redis (`bench start`) before using the site. |
| Tests fail on your site with a custom rule (price approval, HSN, mandatory custom fields) | The fixtures satisfy common ones; run on a staging copy and adapt `tests/utils.py` to your masters. |

## Useful commands

```bash
bench --site <site> console                       # then: frappe.get_doc("Production Plan", "MFG-PP-2026-00001").as_dict()
bench --site <site> execute ppc_production_control.services.sync.refresh_plan --kwargs '{"plan": "MFG-PP-2026-00001"}'
bench --site <site> mariadb                       # tabPPC Audit Log, tabWork Order (production_plan, production_plan_item) …
tail -f logs/worker.log logs/frappe.log           # background jobs / errors
```
Error Log (`/app/error-log`) titles start with `PPC Production Control:` for unexpected failures.

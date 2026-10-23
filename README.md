# PPC Production Control

Production Planning & Control (PPC) for **ERPNext / Frappe v15**, built *on top of the standard
Production Plan*. There is no separate plan document: you plan in ERPNext as usual, and PPC adds

* **automatic draft Work Orders** the moment the plan is submitted (no clicking "Work Order / Subcontract PO"),
* a **Production Control** group in the plan's *Connections* (Job Cards, audit log) and buttons + an embedded
  panel on the plan form,
* a **PPC Production Control screen**: filters, 14 KPIs, Job Card / Work Order tables with bulk submit, and an
  interactive **Gantt chart** with drag-and-drop rescheduling that stays in sync with the Work Orders.

Work Orders and Job Cards remain ordinary ERPNext documents with their normal validation.

> Status: built and tested against ERPNext **15.108.3** / Frappe **15.108.0**. It has **not** been installed on
> a Frappe Cloud site. [docs/VALIDATION_CHECKLIST.md](docs/VALIDATION_CHECKLIST.md) lists what is tested and what
> still needs a check on a real site.

## Contents
1. [How it works](#how-it-works) · [Compatibility matrix](#how-it-fits-erpnext-compatibility-matrix)
2. [Install locally](#install-locally) · [Frappe Cloud](docs/DEPLOYMENT.md)
3. [First-time setup](#first-time-setup) · [Sample workflow](#sample-workflow)
4. [Scheduling rules](#scheduling-rules) · [Roles and security](#roles-and-security)
5. [API](#api) · [Project layout](#project-layout) · [Tests](#running-the-tests)
6. [Known limitations](#known-limitations) · [Troubleshooting](docs/TROUBLESHOOTING.md)

## How it works

```
Standard Production Plan ──submit──▶ (PPC) draft Work Orders, one per plan row, idempotent
        │                                   │ submit (auto, or "Submit Selected Work Orders")
        │                                   ▼
        │                          ERPNext creates the Job Cards
        │                                   │ PPC tags them with the plan, remembers the first schedule
        ▼                                   ▼
 Connections ▸ Production Control   PPC Production Control screen: Gantt · tables · KPIs · audit
```

| Step | What happens |
|---|---|
| Plan submitted | If *Auto Create Draft Work Orders on Submit* is ticked (default), a background job creates the Work Orders by calling the standard `ProductionPlan.create_work_order()` for every row. |
| No duplicates | A plan row maps to at most one live Work Order (`production_plan_item`). The creator skips rows that have one; a guard on Work Order insert also stops the standard **Work Order / Subcontract PO** button from creating a second one for plans using auto-creation. The plan row is locked while creating, so two workers cannot race. |
| Failures | A row that fails is rolled back on its own, written to the audit log (retry status *Pending*) and the other rows continue. **Production Control ▸ Retry Failed** re-runs only what is missing. |
| Submitting | Draft Work Orders are submitted one by one or in bulk through the normal lifecycle; ERPNext's validations (warehouses, your site's own rules…) apply and any failure is reported per record. Optional *Auto Submit Work Orders*. |
| Job Cards | ERPNext creates them when a Work Order is submitted. PPC tags each with the plan, stores its first schedule as the *original* plan, and can schedule cards that have no dates. Only an operation with *no* card at all is generated, with ERPNext's own `make_job_card()`. |
| Rescheduling | Drag/resize on the Gantt (or the date dialog) → validated, confirmed, saved on the Job Card's schedule, Work Order dates synchronised, audit row written. |

## How it fits ERPNext (compatibility matrix)

Everything below was read from the installed ERPNext 15.108.3 source before coding.

| Topic | Standard ERPNext v15 | What PPC does |
|---|---|---|
| Work Orders from a plan | Manual button; `create_work_order` inserts with `ignore_validate=True`; never creates Job Cards | Same code path, triggered automatically on submit, with per-row duplicate protection and audit. Validation happens at submit time, as in the standard flow |
| Job Card creation | `Work Order.on_submit → create_job_card()` | Untouched. PPC links the cards (doc event) and only calls `make_job_card()` for an operation that has none |
| Work Order dates | `planned_start_date`, `planned_end_date`, `expected_delivery_date` are `allow_on_submit` | Updated with a normal `save()` on the submitted document |
| Job Card dates | `expected_start/end_date` are **recomputed from `scheduled_time_logs` on every save** | PPC edits `scheduled_time_logs` (what ERPNext's own capacity planner reads), never just the header |
| Job Card overlap checks | Only run while auto-scheduling | PPC adds its own workstation-capacity check on reschedule |
| Job Card submit | Requires time logs, completed qty, operation order | Unchanged — failures reported per record |
| Other apps overriding Work Order / Job Card | Only one `override_doctype_class` can win | PPC uses `doc_events`, so it coexists |
| New "revised" date fields | — | **None.** Revised = the current ERPNext dates; *Original* = a snapshot PPC stores in two read-only Job Card fields |

**What PPC adds to ERPNext** (custom fields, removed again on uninstall):
`Production Plan`: *Auto Create Draft Work Orders on Submit*, *Auto Submit Work Orders*, *Production Planner*, *Priority*, *Last Scheduling Sync* ·
`Job Card`: *Production Plan* (maintained by PPC), *Original Start / End (PPC)* ·
**No field is added to Work Order** (it already has `production_plan` / `production_plan_item`).
PPC's own DocTypes: *PPC Audit Log*, *PPC Settings*, *PPC Capability Role* (child).

## Install locally

```bash
cd ~/frappe-bench
bench get-app /path/to/ppc_production_control      # or: bench get-app <git-url>
bench --site yoursite install-app ppc_production_control
bench build --app ppc_production_control           # front-end bundles
bench --site yoursite clear-cache
```

Requirements: ERPNext/Frappe v15, Python ≥ 3.10, MariaDB, Redis. A background worker (`bench worker` / `bench start`)
is needed for automatic Work Order creation (it runs as a background job) and for event-driven refreshes;
real-time progress needs the socket.io process. Take a backup first: `bench --site yoursite backup`.

Upgrading from the earlier prototype that had its own plan DocType: `bench migrate` runs a patch that removes
those (empty) DocTypes, their tables and the leftover columns — only if they hold no data.

Frappe Cloud: see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## First-time setup

1. **Roles** (Users): `PPC Manager`, `Production Planner`, `Production Supervisor`, `PPC Viewer`. PPC roles control PPC
   features; ERPNext document permissions still apply, so planners/supervisors also need `Manufacturing User/Manager`
   (and `Sales User` to build plans from Sales Orders). PPC deliberately does not widen anyone's ERPNext access.
2. **Manufacturing Settings**: default *Work-in-Progress* and *Finished Goods* warehouses (ERPNext requires them to submit a Work Order).
3. **PPC Settings** (optional): batch size, Gantt bar limit, capability→role table, planner override, auto-scheduling of cards without dates.
4. Finished goods need an active, submitted, default **BOM** — exactly as for the standard plan.

## Sample workflow

1. **Production Plan ▸ New**: *Get Items From → Sales Order*, add Sales Orders, **Get Finished Goods for Manufacture** — the standard flow.
   Under *PPC Production Control* leave *Auto Create Draft Work Orders on Submit* ticked; optionally set Planner and Priority.
2. **Submit.** Within moments the draft Work Orders exist (see *Connections ▸ Work Order*). If something could not be created,
   **Production Control ▸ Retry Failed** after fixing the cause; the audit log names the row and reason.
3. **Production Control ▸ Open Production Control** (or the embedded panel on the form): *Work Orders* tab → tick drafts →
   **Submit Selected Work Orders** (or tick *Auto Submit Work Orders* up front).
4. ERPNext creates the Job Cards on submit; they appear under *Connections ▸ Production Control ▸ Job Card* and in the *Job Cards* tab.
   **Refresh Job Cards** re-tags/records baselines (idempotent).
5. **Gantt tab**: drag a bar, drag an edge, or double-click for the date dialog. Conflicts are explained and need confirmation;
   planners may *shift following operations*; PPC Managers may *override*.
6. Supervisors record time in ERPNext and use **Submit Selected Job Cards** (earlier operations are submitted first).

## Scheduling rules

* **Job Card**: a *move* shifts every scheduled row by the same amount (keeps ERPNext's working-hour fragmentation); a *resize* collapses to
  one row `[start, end]`. Only `Open`, `Material Transferred`, `On Hold` draft cards can move. Started, `Completed`, submitted or cancelled
  cards are locked, as are cards of Stopped/Closed/Completed/Cancelled Work Orders.
* **Dependencies**: cards of one Work Order are grouped by `sequence_id`; every group must finish before the next starts (same sequence = parallel).
  Cascading preserves duration *and* the gap ERPNext left (~10 min).
* **Workstation capacity**: overlap is a conflict only if peak concurrency exceeds `production_capacity`; back-to-back is fine. Holidays of the
  workstation's Holiday List and the Work Order's delivery date give warnings.
* **Work Order dates**: `planned_start = earliest start`, `planned_end = latest end` of its *remaining* (not completed) cards. Actual dates are never touched.
* **Original vs revised**: *Original* is snapshotted when the card is created (or first seen); *Revised* is its current ERPNext schedule. Cards with no
  schedule show a marked, display-only estimate; *Schedule Unscheduled Operations* writes one (never overwrites an existing schedule).
* **Time zones**: ERPNext stores naive datetimes in the *site* time zone. PPC (server and Gantt) does all arithmetic on naive values, so browser
  time zone and DST cannot shift a schedule.

## Roles and security

| Capability | Manager | Planner | Supervisor | Viewer |
|---|:-:|:-:|:-:|:-:|
| View screen, KPIs, Gantt | ✔ | ✔ | ✔ | ✔ |
| Create draft Work Orders / submit Work Orders / generate Job Cards | ✔ | ✔ | | |
| Submit Job Cards | ✔ | ✔ | ✔ | |
| Reschedule | ✔ | ✔ | | |
| Override conflicts | ✔ | (setting) | | |
| Retry failed operations | ✔ | ✔ | | |

Editable in *PPC Settings → Capability to Role Mapping* (System Manager always allowed). Every API call checks the capability **and** the
document permission. Also: bound SQL parameters only; whitelisted sort columns; POST-only mutators (CSRF); generic error text with the real
error in the Error Log; append-only audit rows (links from the audit log never block deleting a Work Order); bulk actions only touch documents
that belong to the plan.

## API

`ppc_production_control.api.*` (`/api/method/…`): `create_work_orders`, `sync_work_orders`, `generate_job_cards`, `refresh_job_cards`,
`get_job_cards`, `get_work_orders`, `get_gantt_data`, `submit_selected_work_orders`, `submit_selected_job_cards`, `validate_schedule`,
`reschedule_job_card`, `sync_work_order_schedule`, `schedule_unscheduled`, `get_ppc_dashboard`, `get_plan_overview`, `get_audit_log`,
`retry_failed_operations`, `get_capabilities`. Partial-success endpoints return `results: [{name, ok, skipped, message}]`.

## Project layout

```
ppc_production_control/
├── pyproject.toml · README.md · license.txt
├── docs/                       DEPLOYMENT · TROUBLESHOOTING · VALIDATION_CHECKLIST
└── ppc_production_control/
    ├── hooks.py                doc_events, dashboard override, doctype_js, scheduler, install hooks
    ├── install.py              roles, custom fields, default settings (idempotent; runs on every migrate)
    ├── events.py               auto draft Work Orders on submit, duplicate guard, tag Job Cards, queue refresh
    ├── tasks.py                hourly self-healing refresh
    ├── api.py                  whitelisted endpoints (capability + document permission on each)
    ├── overrides/production_plan_dashboard.py    "Production Control" group in the plan's Connections
    ├── patches/                remove the prototype's plan DocTypes · re-tag Job Cards
    ├── services/
    │   ├── work_orders.py      draft creation (idempotent), submit, bulk submit, retry
    │   ├── job_cards.py        generate missing cards, bulk submit
    │   ├── sync.py             tag cards with the plan, baselines, auto-schedule
    │   ├── scheduling.py       validation, reschedule, cascade, Work Order sync, auto-schedule
    │   ├── queries.py          filtered Job Card / Work Order / Gantt / KPI queries
    │   └── audit.py · permissions.py · constants.py · utils.py
    ├── ppc_production_control/
    │   ├── doctype/            PPC Audit Log · PPC Settings · PPC Capability Role
    │   ├── page/ppc_production_control/   the control screen
    │   └── workspace/ppc/      workspace shortcuts
    ├── public/
    │   ├── gantt/ppc_gantt.js  dependency-free Gantt (MIT, no third-party code)
    │   ├── js/                 ppc_common · ppc_widgets · ppc_production_plan (form) · ppc_control.bundle.js
    │   └── css/ppc.bundle.css
    └── tests/                  Python (FrappeTestCase) + tests/js (Gantt, Node)
```

**Why a custom Gantt?** Frappe ships frappe-gantt 0.6 (MIT) without row grouping, a frozen task list, a quarter scale or sub-day snapping — all needed here.
Writing it in-app means no third-party version or licence to pin, nothing extra on Frappe Cloud, and full control of the time-zone-safe maths.

## Running the tests

```bash
# Python — integration tests against a real site (each test rolls back; nothing is left behind)
bench --site yoursite run-tests --app ppc_production_control --skip-test-records --skip-before-tests
#   or one module:  --module ppc_production_control.tests.test_planning

# JavaScript — Gantt maths + DOM (dev-only dependency: jsdom)
cd ppc_production_control/ppc_production_control/tests/js && npm install && node --test
TZ=Pacific/Auckland node --test        # also try other time zones
```
Use a **test/staging site**: the tests create masters (items, BOM, Sales Orders…) inside a transaction that is rolled back, but they do run your
site's hooks and server scripts. If your company is not `_Test Company`, set `"ppc_test_company": "<name>"` in `site_config.json`.

## Known limitations

* **Consolidated plans.** With ERPNext's *Consolidate Items*, plan rows carry no Sales Order, so those Work Orders have none either; the control
  screen's Sales Order / Customer filters cannot match them (Plan, Item, Work Order, Workstation… filters work).
* **Sub-assembly rows** of type *Subcontract* (Purchase Orders) and *Material Request* stay with the standard buttons; PPC creates the in-house Work Orders only.
* **Validation timing.** Auto-created Work Orders are made by the standard `create_work_order` (which skips validation to keep drafts creatable), so problems such as a
  missing warehouse surface when the Work Order is *submitted* — reported per record, never bypassed.
* **Standard "Work Order / Subcontract PO" button** stays visible; for plans using auto-creation it can no longer duplicate Work Orders (the guard refuses), but it still creates subcontract POs.
* **Work Order Operation** planned times are read-only after submit in ERPNext; PPC updates the Work Order header dates and Job Cards only.
* A **resize** collapses ERPNext's working-hour fragments into one row; a *move* keeps them. Rescheduling does not re-run ERPNext's capacity planner or
  working-hours calendar (holidays only warn). Moving a bar vertically (changing workstation) is not supported.
* Conflict detection scans the filtered scope; on very large scopes narrow the filters (Gantt: 500 bars by default; table: 200 rows/page; "select all matching": 10 000).
* Real-time progress needs socket.io; without it the screen updates on reload.
* Only tested on ERPNext 15.108.x; not tested with serial/batch-tracked finished goods, subcontracting, or product bundles.

More: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md), [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## License
MIT — see `license.txt`.

# Deploying PPC Production Control

> Not tested on your live Frappe Cloud site. Follow the staging steps first.

## Requirements and Frappe Cloud notes

* ERPNext **v15** bench group on Frappe Cloud (the app declares `required_apps = ["erpnext"]`).
* **No shell, root access, external executables or server-filesystem access** is required or used.
  Everything is Python + the bundled front-end build that Frappe Cloud runs for you (`bench build`).
* Background workers and the scheduler are part of every Frappe Cloud site; PPC uses the `long`/`short`
  queues (Work Order creation and refresh jobs) and one hourly scheduler job.
* Custom fields, roles and default settings are created by `install.py` on **install and on every migrate**
  — idempotent, so deploys are safe to repeat. No fixture files to export.
* Plan limits: a very small plan with tight RAM may time out on huge plans; PPC processes rows one at a time and commits per row.
  If your plan restricts private GitHub apps, you need a GitHub-connected team (Frappe Cloud → Settings → GitHub).

## 1. Put the code in Git

```bash
cd apps/ppc_production_control
git init -b main            # already done locally; skip if it is a repository
git add . && git commit -m "PPC Production Control 1.0.0"
git remote add origin git@github.com:<you>/ppc_production_control.git
git push -u origin main
```
`public/dist/` and `node_modules` are git-ignored on purpose (built on deploy).
Tag releases (`git tag v1.0.0 && git push --tags`) so you can redeploy a known version.

## 2. Try it on a staging site first (strongly recommended)

1. Frappe Cloud → create a **new site** on a *test bench* (or restore a recent backup of production onto one).
2. Bench → **Apps → Add App → From GitHub** → choose the repo and branch → **Deploy** (wait for the build).
3. Site → **Apps → Install App → ppc_production_control**.
4. Run the manual script in [VALIDATION_CHECKLIST.md](VALIDATION_CHECKLIST.md).

## 3. Production deployment

1. **Backup**: Site → Backups → *Schedule/Create backup* (database **and** files). Download a copy.
2. Bench → Apps → Add App (same repo/branch) → Deploy. Frappe Cloud builds a new bench image; your site is
   moved to it automatically during the deploy window. Migration runs `after_migrate`, which creates the roles
   and the custom fields.
3. Site → Apps → **Install** `ppc_production_control` (first time only).
4. Open **PPC Settings**; give users the PPC roles; check *Manufacturing Settings* defaults (WIP / FG warehouse).
5. Open `/app/ppc-production-control`; create a small plan from one real Sales Order, with a BOM you can test with.

What the install changes in your database (all additive):
* 4 new DocTypes (*PPC Audit Log*, *PPC Schedule Revision*, *PPC Settings*, *PPC Capability Role*), the *PPC Production Control* page and the *PPC* workspace.
* 4 new Roles, and one new capability row (*Reconcile Schedules* → PPC Manager) in PPC Settings.
* Custom Fields: `Production Plan` → `ppc_auto_create_work_orders`, `ppc_auto_submit_work_orders`, `ppc_planner`, `ppc_priority`,
  `ppc_last_scheduling_sync` (plus a section/column break); `Job Card` → `ppc_production_plan`, `ppc_original_start`, `ppc_original_end`
  and, new in 1.1, the read-only `ppc_baseline_source`, `ppc_baseline_captured_at`, `ppc_baseline_captured_by`, `ppc_revised_start`, `ppc_revised_end`,
  `ppc_schedule_revision`, `ppc_last_rescheduled_at`, `ppc_last_rescheduled_by`, `ppc_schedule_sync_status`. **Nothing is added to Work Order.**
* A "Production Control" group in the Production Plan's Connections, and a script on the Production Plan form.
* Document events: *Production Plan* `on_submit` (queues the automatic draft Work Orders), *Work Order* `before_insert` (duplicate guard, only for plans
  using auto-creation), *Job Card* `before_insert/after_insert/validate/on_update/on_submit/on_cancel` (tagging, baseline capture, a guard that refuses edits
  of the original schedule, refresh) and one hourly scheduler job.
It does **not** change Item, BOM, Sales Order, or any ERPNext permission, and does not alter other apps' overrides.

## Migration and rollback plan

| Situation | Action |
|---|---|
| Something unexpected right after install | Site → Apps → **Uninstall** `ppc_production_control`. The `before_uninstall` hook removes the custom fields; Production Plans, Work Orders and Job Cards are untouched. Roles stay (they may be assigned). Switching off *Auto Create Draft Work Orders on Submit* on a plan stops the automation for that plan immediately. |
| Need the old state exactly | Restore the pre-install backup (**Site → Backups → Restore**). Note: this discards *everything* since the backup. |
| Bad release of the app | Redeploy the previous Git tag/commit (Bench → Apps → select the older commit → Deploy). Data created by PPC remains valid; link tables are plain tables. |
| Cancel a plan | Standard ERPNext behaviour: draft Work Orders of the plan are deleted; submitted ones must be cancelled first. |
| Upgrading from 1.0.0 | `bench migrate` (automatic on Frappe Cloud deploys) adds the DocType and fields and runs the idempotent patch `backfill_schedule_history`, which only *adds* revision-0 rows for Job Cards that already carry a 1.0.0 baseline. After the deploy open the Job Cards tab → **Check Schedule Consistency**, then **Initialize Missing Baselines** (preview first) for older cards that have a schedule but no baseline. |
| Roll back 1.1 → 1.0.0 | Redeploy the 1.0.0 commit. The new fields and the *PPC Schedule Revision* table stay in the database, unused and harmless; redeploying 1.1 later picks the history up again. **Do not uninstall the app to "roll back"**: uninstalling drops the *PPC Schedule Revision* DocType and its history — export it first: `bench --site <site> export-csv "PPC Schedule Revision" revisions.csv`. |
| A reschedule failed half way | It cannot: schedule, Work Order dates and revision are one transaction. After a rollback the card is flagged *Failed*, the proposal is kept in the audit log, and **Retry Failed Synchronizations** replays it with re-validation. |
| Fields were removed but PPC reinstalled later | `bench migrate` (automatic on deploy) recreates the custom fields; the migrate patch `retag_job_cards` re-tags Job Cards of existing plans (or run *Refresh Job Cards* on each plan). |

Data-safety design: Work Orders and Job Cards are never modified directly in SQL (PPC only writes its own custom fields on them);
removing the app never deletes manufacturing documents.

## Local development bench

```bash
bench get-app /path/to/ppc_production_control
bench --site dev.local install-app ppc_production_control
bench build --app ppc_production_control        # re-run after JS/CSS changes
bench --site dev.local migrate                  # after changing DocType JSON
bench --site dev.local run-tests --app ppc_production_control --skip-test-records --skip-before-tests
```
Upgrade ERPNext/Frappe on a staging copy and re-run the tests before upgrading production.

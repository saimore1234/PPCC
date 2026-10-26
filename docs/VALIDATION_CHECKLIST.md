# Validation checklist

## 1.2.0 — PPC Control Center (branch `feature/control-center-ui`)

Tested on `dev.local` (Python, each test rolled back; **no migrate**) and on the disposable staging copy `stage.local` (migrated, browser tests). Edge only.

| Layer | Where | Passed | Failed |
|---|---|--:|--:|
| Python integration tests, 11 suites (194 tests: the 10 below plus `test_control_center` 45) | `dev.local` | **194** | 0 |
| JavaScript: Gantt maths + DOM + Control Center UI logic, 5 time zones | – | **55** per zone | 0 |
| Browser: Control Center UI (workspace, navigation, KPI drill-downs, filters, pagination, bulk submit, Gantt controls, permissions, light/dark contrast, widths 390–1440, keyboard) | staging | **22 / 22** | 0 |
| Browser: 19-step schedule/sync flow on the new pages | staging | **19 / 19** | 0 |
| Browser: drag regression (10 cycles per variant) | staging | **30 / 30** | 0 |

Defects found by these runs and fixed: a first request sent before the filters were applied (Frappe control values apply asynchronously); a `setTimeout` "Illegal invocation" in the debounce helper; a plain click on a movable Gantt bar never selected it (the chart was rebuilt under the pointer; now covered by a DOM test that fails on the old code).
Not verified: other browsers; a fresh install of 1.2.0 (only an upgrade of a 1.1.0 site was run); Frappe Cloud; real production data.

## 1.1.0 — branch `feature/schedule-revisions` (backend unchanged in 1.2.0)

Legend: ✅ verified by an automated test that **passed** · 🖥 verified by the automated browser tests (Edge) · 🧪 verified on a disposable staging copy ·
⚠ **not verified — must be tested on your real site** · ➖ handled by the standard ERPNext Production Plan, not by PPC.

Tested against: ERPNext 15.108.3, Frappe 15.108.0, MariaDB 10.6, Python 3.11, on a development site (`dev.local`) and on a disposable copy of it
(`stage.local`, restored from a **1.0.0-state backup** into a private MariaDB), both running `india_compliance`, `hrms` and customer apps including
`chemical_mfg` (which overrides Work Order / Job Card). **Nothing was installed on or tested against Frappe Cloud or on real production data.**

## Results of the final runs

| Layer | Where | Passed | Failed | Skipped |
|---|---|--:|--:|--:|
| Python integration tests, 10 suites | staging copy, after the upgrade | **149** | 0 | 0 |
| Python integration tests, 10 suites | `dev.local`, final code | **149** | 0 | 0 |
| JavaScript (Gantt maths + DOM), 5 time zones | – | **40** per zone | 0 | 0 |
| Browser: 19-step end-to-end | staging, final code, two consecutive runs | **19 and 19** | 0 | 0 |
| Browser: drag-interaction regression (10 cycles per timing variant) | staging, final code | **30** | 0 | 0 |
| Upgrade 1.0.0 → 1.1.0: 21 checks | staging, three times (twice from a fresh 1.0.0 database) | **21 / 21** each time | 0 | – |

Suite sizes: planning 15 · work orders 7 · job cards 12 · scheduling (pure) 12 · scheduling 22 · queries 16 · API security 11 · revisions 23 · reconcile 25 · chemical_mfg compatibility 6.

## The intermittent Gantt drag issue — what it was

**Symptom.** In the browser test, the first drag after a "Schedule changed by someone else" refusal sometimes sent no request; the next drag worked.

**Root cause (reproduced on demand, `ppc_drag_regression.js` → "ROOT CAUSE … characterisation").** The refusal dialog is a Bootstrap modal. While it is still
fading in (about 0.3 s) Bootstrap ignores every attempt to hide it - Escape, the close button, an overlay click. The old test pressed Escape the instant the dialog
appeared (ignored), went on to read the database for a few seconds, and then dragged: the drag landed on the still-open dialog's overlay, so **no request was sent**,
and the click dismissed the dialog - which is why the following drag worked. With the dialog's fade slowed to 2 s the sequence is deterministic. It is **standard
dialog behaviour and a test-synchronisation defect, not a lost request in the app**: with every wait based on observable state (never a fixed delay and never a
retry) the drag after a refusal sent exactly one request and was saved every time: **60 of 60** on the hardened Gantt in the three final 20-cycle runs (plus every
cycle of the shorter runs), and **12 of 12** on the pre-hardening Gantt in the A/B run.

**What else the investigation found and changed (all covered by tests):**

| Finding | Evidence | Change |
|---|---|---|
| Escape pressed while a save was in flight cancelled the drag state (the answer then hit a cleared state) | jsdom test failed before the fix | Escape only abandons a drag in progress |
| A new drag could start while the previous save was unanswered; the first save's answer then ended the *second* drag | jsdom test failed before the fix | one change at a time; an answer only ends its own drag |
| A refresh that re-rendered the chart mid-drag or mid-save left the visible bar idle and the dragged bar detached | jsdom tests failed before the fix | the new bar element keeps the dragging / saving look and position |
| No observable state for tests or support | – | `data-ppc-drag` (idle / dragging / pending) and `data-ppc-busy` (loads in flight) |
| A browser took the pointer away (`pointercancel`) and the drag stayed "dragging" | by inspection | handled |

*Honest caveats:* an A/B run of the **pre-hardening** Gantt through the whole browser regression passed too, so the first four rows are real code weaknesses proven in
jsdom but were **not observed to hurt a user in the real browser** (Frappe's freeze overlay blocks interaction during a save, and Edge keeps delivering pointer events
after a re-render). They are defence in depth. The measured server round trip was 0.3-0.7 s median; one drag in ~40 took 10 s on this slow 4 GB machine.

**Test defects found while building the regression (all fixed, listed for honesty):** an assertion treating the ordinary hover tooltip as a leftover; slowing the
dialog's *backdrop* delayed the dialog itself; a "moves before the predecessor" drag that stopped crossing the predecessor after earlier cycles had drifted the bar
(now computed from the data, and the cycles alternate direction so the bar returns to where it started - otherwise it passes the Work Order's delivery date and the
server, rightly, asks for confirmation); Playwright route cleanup that crashed the script (cleanup now always runs); a `page.goto` that timed out on the slow machine
(larger navigation timeouts - they never affect how drags are synchronised).

## Migration 1.0.0 → 1.1.0 🧪

Procedure: [STAGING_RUNBOOK.md](STAGING_RUNBOOK.md). A private MariaDB (port 3307) + site `stage.local` restored from a backup taken while the app was still 1.0.0
(282 Job Cards, 47 with a 1.0.0 baseline, 13 of them moved since; no revision table).

| Step | Result |
|---|---|
| `bench migrate`, attempt 1 | **FAILED** - `Unknown column 'ppc_schedule_revision'`. **Real bug**, not visible on `dev.local` (schema applied by hand there): patches run *before* `after_migrate`, which creates the new Job Card fields. The failed run left the data untouched (checksum unchanged, patch not logged) |
| Fix | the patch now creates the fields itself (idempotent) before using them; regression test `test_migration_patch_creates_the_fields_it_needs_before_it_uses_them` |
| `bench migrate`, attempt 2 (from the half-migrated state) | exit 0; `PPC: added schedule history to 47 Job Card(s)` |
| 21 checks (`tests/migration/verify_upgrade.py`) | **21 / 21**: DocType + 9 columns; `ppc_original_*` **byte-identical** (sha256) for all 47 cards; every legacy card has revision 0 equal to its 1.0.0 baseline; the 13 moved cards got exactly one *Migration* revision saying author/time/reason "were not recorded"; **`changed_by` empty on every migrated row (nothing invented)**; accepted-schedule pointers and counters consistent; revision rows = 47 + 13 = 60; cards without a baseline untouched (163 remain for the preview-first button); audit log unchanged; patch idempotent; PPC Manager can reconcile without a settings row; no Work Order column added |
| `bench migrate`, attempt 3 | exit 0, nothing for PPC to do; state identical to attempt 2; 21 / 21 again |
| **Rollback A - restore** | restoring the 1.0.0 dump returned the database to **exactly** the pre-upgrade snapshot (no revision DocType, no new columns, sha256 equal) |
| **Rollback B - redeploy 1.0.0** | 1.0.0 code (exported with `git archive`, first on `PYTHONPATH`) + its own 95 tests against the *upgraded* database: **92 / 95 pass**; the 3 failures (`test_queries`: `test_date_filters`, `test_each_filter`, `test_overdue_unassigned_and_conflict_flags`) are **identical on the pre-upgrade database** (control run after the restore) - those old tests are unscoped and see the copy's real Job Cards; 1.1 fixed them by scoping. So the schema upgrade does not affect 1.0.0 |
| Second upgrade, from the restored 1.0.0 database, single attempt | exit 0, 47 cards backfilled, 21 / 21, state identical to the first upgrade |
| `chemical_mfg` | installed on the copy; compatibility suite 6 / 6 after the upgrade; full browser run on the upgraded copy passes |

Not done: `bench migrate` on `dev.local` or any live site (not approved). ⚠ A real production dump may differ from this copy (more cards, other apps' patches) - run
the runbook on it before deploying.

## Release-readiness review

| Area | Evidence | Status |
|---|---|---|
| Schedule-revision audit integrity | revision rows append-only (no create/edit/delete by normal means; audited correction annotates only); revisions created only inside the reschedule transaction; failed/refused/stale changes leave no revision; history outlives deleted cards; migration invents no author; `check_consistency` flags duplicates/holes/mismatches and never rewrites them | ✅ 🧪 |
| Work Order date sync | Work Order window = earliest start / latest end of remaining Job Cards; read back and verified in the same transaction; rolled back on failure; two-browser race: exactly one change applied; browser tests assert Work Order dates after every move | ✅ 🖥 |
| Permissions | every endpoint checks capability and document permission; view-only user gets 403 on all five mutating endpoints (browser); new capability *Reconcile Schedules* falls back to PPC Manager on upgraded sites (tested) | ✅ 🖥 |
| Frappe Cloud compatibility | `required_apps = ["erpnext"]`, Python ≥ 3.10, flit packaging, dynamic version, built assets not committed, patches idempotent and ordered, no use of developer-only features | reviewed; ⚠ never deployed there |
| Rollback | restore and redeploy-1.0.0 both demonstrated above; do **not** uninstall the app to roll back (drops the history table) | 🧪 |

## Not verified / known open items ⚠

* **Frappe Cloud**, and **your production data** (the staging copy is a copy of the *dev* site).
* The **Linux/Chrome** browsers (only Windows Edge was used), dark theme, mobile layout, socket.io progress.
* Real **parallel load** beyond two browsers; very large Job Card counts (the check is capped at 5 000 cards); thousands of open cards on the Gantt.
* One drag in ~40 took ~10 s on the test machine - a slow server is tolerated, but a persistently slow `reschedule_job_card` on your hardware is worth measuring.
* The 3 old 1.0.0 `test_queries` tests are data-sensitive (fixed in 1.1; relevant only if you roll back).

## Exact test commands

```bash
# Python (each test rolls back; the site is left unchanged). Use --site <staging> to be safe.
for m in test_planning test_work_orders test_job_cards test_scheduling_pure test_scheduling test_queries test_api_security test_revisions test_reconcile test_chemical_mfg_compat; do
  bench --site <site> run-tests --module ppc_production_control.tests.$m --skip-test-records --skip-before-tests
done
# JavaScript, five time zones
cd ppc_production_control/ppc_production_control/tests/js && npm install
for tz in UTC Europe/Berlin America/New_York Asia/Kolkata Pacific/Auckland; do TZ=$tz node --test; done
# Browser (commit disposable data and remove it; need a background worker) - run on a staging copy
cd ../e2e && npm install
export PPC_E2E_SITE=<site> PPC_E2E_BASE=http://localhost:<port> PPC_E2E_RUNNER="bash $PWD/run_fixture.sh" PPC_E2E_BROWSER=/path/to/chrome
node ppc_e2e.js                                  # the 19-step flow
PPC_E2E_CYCLES=10 node ppc_drag_regression.js    # the drag-interaction regression
# Upgrade check on a copy of a 1.0.0 site: docs/STAGING_RUNBOOK.md (tests/migration/*.py)
```

## Manual staging steps (30 minutes)

1. Back up, deploy the branch to **staging**, run `bench --site <site> migrate`; expect `PPC: added schedule history to N Job Card(s)` and no error; run
   `tests/migration/verify_upgrade.py` against a pre-upgrade snapshot (see the runbook).
2. Job Cards tab → **Check Schedule Consistency**; then **Initialize Missing Baselines** (read the preview) for older cards.
3. Run one real Sales Order through plan → draft Work Orders → submit → Job Cards, drag one operation; confirm the Work Order's planned dates and the Job Card's
   *Scheduled Time Logs* changed, the Gantt shows the ghost bar and `R1`, and the detail panel lists the revision.
4. Two tabs on the same card: drag in one, then in the other → "changed by someone else", nothing saved; close the dialog (wait until it has finished appearing) and drag again.
5. Make Work Order saves fail (a validation script) → drag → bar snaps back, card `⚠ Failed`, audit row Failed; remove the cause → **Retry Failed Synchronizations**.
6. As `PPC Viewer`: everything visible, nothing changeable; as `Production Planner`: reschedule works, *Initialize/Repair* absent.
7. Edit a Job Card's time log by hand in ERPNext → **Check Schedule Consistency** shows *schedule drift* → preview → repair.

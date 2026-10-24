# Validation checklist (version 1.1.0 — branch `feature/schedule-revisions`)

Legend: ✅ verified by an automated test that **passed** · 🖥 verified by the automated browser test (Edge, development server) ·
⚠ **not verified — must be tested on your real site** · ➖ handled by the standard ERPNext Production Plan, not by PPC.

Tested against: ERPNext 15.108.3, Frappe 15.108.0, MariaDB, Python 3.11, on a development copy (`dev.local`) that also runs
`india_compliance`, `hrms` and several customer apps (including `chemical_mfg`, which overrides Work Order / Job Card).
**Nothing was installed on or tested against Frappe Cloud.**

## Results of the last full run

| Layer | Passed | Failed | Skipped |
|---|--:|--:|--:|
| Python integration tests (10 suites, one real site, each test rolled back) | **147** | 0 | 0 |
| JavaScript (Gantt maths + DOM), run in 5 time zones (UTC, Berlin, New York, Kolkata, Auckland) | **33** per zone | 0 | 0 |
| Browser end-to-end (Playwright + Edge, real committed data, cleaned up afterwards) | **19** | 0 | 0 |

Honest history of the browser test: run 1 → 16/19 (three defects in the *test script*: a selector matching two checkboxes, a case-sensitive
match against CSS-upper-cased labels, and a dialog left open by the first failure); run 2 and 3 → 18/19 (the drag that follows a stale-token
refusal was not registered by the browser the first time); run 4 → 19/19 after the test was given a diagnostic screenshot and one retry of that drag.
**Open observation:** in runs 2–4 (diagnosed in 3 and 4) the first drag after "someone else changed this card → the screen reloads itself" sent no request (the bar was
selected, no dialog or overlay was open) and worked on the second attempt. It may be automation timing or a real first-drag-after-reload glitch;
it has not been root-caused. Nothing is lost either way (the refused change had saved nothing).

| Suite | Tests | Covers |
|---|--:|---|
| `test_planning` | 15 | standard plan with PPC extensions; **draft Work Orders created on submit**; option off; queued; **no duplicates**; standard button guard; failing row isolated, audited, retried; auto-submit + Job Cards; plan cancel |
| `test_work_orders` | 7 | submit, **submit failure reported not bypassed**, **bulk submit**, partial failure, cancelled never submitted, cancel/delete not blocked, auto-submit |
| `test_job_cards` | 12 | cards follow BOM operations and carry the plan; **no duplicates**; only the missing operation regenerated; re-tag reports (never invents) baselines; **bulk submit**; ERPNext failures reported; batch limit |
| `test_scheduling_pure` | 12 | concurrency sweep, sequence groups, cascade, layout |
| `test_scheduling` | 22 | reschedule move/resize, Work Order date sync, audit trail, actual dates untouched, invalid dates, dependency violations, workstation capacity, protection of started/completed/cancelled cards, locked Work Order, stale write, holiday/delivery warnings, **midnight / month-end / leap-day / year boundaries**, auto-schedule |
| **`test_revisions`** | **23** | **baseline at card creation (revision 0), baseline immutable (guard + re-capture no-op), "initialized from existing schedule" labelling, revisions 1 and 2 with previous/proposed/accepted, earlier/later, start-only/end-only, Work Order = aggregate of several Job Cards, cascade with shared correlation id, refused/locked moves leave no revision, actual dates separate from schedule, atomic rollback when the Work Order sync fails / the history write fails / ERPNext did not store the dates (nothing changes, card flagged Failed, proposal stored), retry replays the stored proposal and resolves it, stale token rejected, date boundaries, history view + difference text + Gantt/list data match what is persisted, history append-only (no manual create/edit/delete), audited correction only annotates, history outlives a deleted draft, permissions** |
| **`test_reconcile`** | **23** | **each issue type detected (missing baseline, missing history, card drift, Work Order drift, failed sync, revision mismatch, orphan Work Order); check is read-only; initialize baselines preview vs apply, idempotent, never overwrites; repair preview changes nothing, repair re-applies the accepted schedule as a new revision, only the requested types, idempotent; finished work never changed; Work Order still spanning a finished operation is not reported; upgrade of 1.0.0 cards (history added, baseline untouched, explanatory "Migration" revision with unknown author not invented, patch idempotent, cards without baseline left alone); permission-controlled; endpoints default to preview** |
| **`test_chemical_mfg_compat`** | **6** | both apps' `doc_events` coexist; Job Cards follow the BOM exactly once; **quality template survives a reschedule**; no recursion and exactly one audit row + one revision per reschedule; Work Order sync through the other app's Work Order class; its sequence rule still applies and leaves no revision (skipped, not failed, when `chemical_mfg` is absent) |
| `test_queries` | 16 | filters, PPC-only scope, pagination, sort whitelist, Gantt cap/truncation/dependencies, KPIs, workload, Work Order list |
| `test_api_security` | 11 | roles and capabilities, ERPNext permissions still enforced, SQL-injection inertness, error masking, POST-only endpoints |
| `tests/js/gantt_math` + `gantt_dom` | 33 | maths in 5 zones; render, scales, drag/resize/cancel/reject→revert, locked/read-only, grouping, search, escaping, server-clock "today"; **ghost baseline bar, actual-execution line, revision badge, sync warning, schedule columns, no-baseline dashes, selection callback, escaping of the new columns** |

## Browser end-to-end test (🖥) — what the 19 steps prove

`tests/e2e/ppc_e2e.js`, real browser, real server, real background worker, as a **PPC Manager without the System Manager role** and then a **PPC Viewer**:
standard Sales Order → standard Production Plan submitted → the worker creates exactly one **draft Work Order** → submitted in bulk from the PPC screen →
ERPNext creates one Job Card per operation, each with a **revision-0 baseline** → Job Cards tab shows the new columns → Gantt schedule columns →
**drag** (real schedule saved, one time row, Work Order dates = aggregate, revision 1, ghost bar, badge) → **page reload shows the persisted dates** →
**resize** (revision "Gantt Resize") → **move into the successor** (confirmation dialog, cascade, one correlation id, Work Order window) →
**impossible move refused/cancelled** (nothing changed, bar snaps back) → **stale view rejected** (nothing saved) → detail panel with history →
**out-of-band edit detected, preview changes nothing, repair adds a "Reconciliation" revision, second check clean** → audit tab →
**view-only user**: no handles, no request sent, and `reschedule_job_card`, `repair_schedule_inconsistencies`, `initialize_missing_baselines`,
`retry_failed_synchronizations`, `correct_schedule_revision` all answer **HTTP 403** → no browser console errors / HTTP 5xx → **cleanup removed every record** (verified by count).

Disposable data: items/BOM/workstations/operations/Sales Order/Plan/Work Order/Job Cards named `PPCE2E…` in the site's own company, two throw-away users.
It consumes a few numbers from the site's naming series (gap) and writes nothing to Manufacturing Settings or other global settings.
Not exercised by it: a Job Card with real time logs/actual dates in the browser (covered by Python tests and the jsdom actual-line test), and a *forced*
infrastructure failure during a browser reschedule (covered by the Python atomicity tests with a mocked failure).

## Not run / not verified (failed-or-skipped report)

* **Skipped tests:** none. **Failed tests:** none in the last run (see the history above for the earlier browser-script failures).
* **Frappe Cloud** — never installed there. ⚠
* **Dark theme, mobile layout, socket.io progress** — not looked at in the browser.
* **`bench migrate` of a real 1.0.0 → 1.1.0 upgrade** — the patch's logic is tested, and its query ran against the dev site's 56 real legacy Job Cards (inside a test transaction that was rolled back: it
  reported 56 cards to backfill and no real data changed), but it has **not** been executed through a real `bench migrate` — I deliberately did not
  run `migrate` on the shared dev site, which would also run other apps' pending patches. ⚠ Run it on a staging copy first.
* **Concurrency under real parallel load** — row locks and the stale token are tested sequentially; two simultaneous reschedules of one card were not stress-tested. ⚠
* **Volume** — thousands of open Job Cards, 500-bar Gantt, large reconciliation (capped at 5 000 cards per check). ⚠
* **Your site's own rules** — workflows/Server Scripts on Work Order or Job Card, serial/batch finished goods, subcontract rows, product bundles, consolidated plans. ⚠
* The e2e test needs a **background worker**; without one the first step times out by design.

## Manual staging steps (30 minutes)

1. Backup (`bench --site <site> backup --with-files`), deploy the branch to **staging**, run `bench --site <site> migrate`; check the output for
   `PPC: added schedule history to N Job Card(s)` and that `bench build` completed.
2. Open the Job Cards tab → **Check Schedule Consistency**. Expect only issues you understand. **Initialize Missing Baselines** → read the preview → apply.
3. Run one real Sales Order through plan → draft Work Orders → submit → Job Cards, then drag one operation; confirm the Work Order's planned dates and the
   *Job Card → Scheduled Time Logs* table changed, the Gantt shows the ghost bar and `R1`, and the detail panel lists the revision.
4. Drag with someone else editing the same Job Card in another tab → you must get "changed by someone else" and nothing saved.
5. Temporarily make Work Order saves fail (e.g. a validation script) → drag → bar snaps back, card shows `⚠ Failed`, audit log has a Failed row; remove the cause →
   **Retry Failed Synchronizations** → applied as a new revision.
6. As a `PPC Viewer`: everything visible, nothing changeable; as `PPC Planner`: reschedule works, *Initialize/Repair* buttons absent.
7. Edit a Job Card's time log by hand in ERPNext → **Check Schedule Consistency** reports *schedule drift* → preview → repair.

## Exact test commands

```bash
# Python (all suites; each test rolls back, the site is left unchanged)
for m in test_planning test_work_orders test_job_cards test_scheduling_pure test_scheduling test_queries test_api_security test_revisions test_reconcile test_chemical_mfg_compat; do
  bench --site dev.local run-tests --module ppc_production_control.tests.$m --skip-test-records --skip-before-tests
done
# JavaScript, five time zones
cd ppc_production_control/ppc_production_control/tests/js && npm install
for tz in UTC Europe/Berlin America/New_York Asia/Kolkata Pacific/Auckland; do TZ=$tz node --test; done
# Browser end-to-end (commits disposable data, removes it afterwards; needs a worker) — see README "Running the tests"
cd ../e2e && npm install && PPC_E2E_RUNNER="bash $PWD/run_fixture.sh" PPC_E2E_BROWSER=/path/to/chrome node ppc_e2e.js
```

# Baseline, current schedule, actual dates and revision history

This document explains how PPC keeps four different things apart, and what is guaranteed when a bar is dragged on the Gantt.

## 1. Four different things

| Concept | Where it lives | Who writes it | Can it change? |
|---|---|---|---|
| **Original (baseline) schedule** | Job Card `ppc_original_start/end` (read-only) **and** revision 0 of its history | PPC, once, when ERPNext first gives the Job Card a schedule | **Never.** A `validate` hook refuses any edit; capturing again is a no-op |
| **Current (revised) schedule** | The Job Card's real ERPNext schedule: `scheduled_time_logs` → `expected_start_date` / `expected_end_date` | PPC, through an accepted reschedule | Yes, only through an accepted, audited reschedule |
| **Last accepted schedule** | Job Card `ppc_revised_start/end` (read-only mirror) | PPC, in the same transaction as the schedule | Yes — it exists so that a later divergence is *detectable* |
| **Actual execution** | Job Card `actual_start_date` / `actual_end_date` (ERPNext, from the time logs) | ERPNext | PPC **never** writes it |

Plus the **revision history**: one append-only `PPC Schedule Revision` row per accepted change.

On the Gantt: the coloured bar is the *current* schedule, the thin grey line under it is the *original* plan (drawn only when it differs),
and the thin green line under that is the *actual* execution (dashed while an operation is still running). A badge `R2` on the bar says
"rescheduled twice"; `⚠` says the last synchronisation failed or the card drifted from its accepted schedule.

### What "original" means for cards that existed before this feature
A Job Card that had a schedule but no recorded baseline gets one when you run **Initialize Missing Baselines** (preview first). Its *current*
schedule at that moment is recorded and **labelled** *"Baseline initialized from existing schedule"* — PPC cannot know what the true original
was, so it does not pretend. Cards that carried PPC 1.0.0's "original" snapshot keep exactly those values; the migration only adds the missing
history rows (see *Upgrading*).

## 2. A revision row

`PPC Schedule Revision` (naming `PPC-REV-#########`, read-only for everybody, no delete):

`revision_no` · type (*Baseline* / *Revision*) · **previous** start/end · **proposed** start/end · **accepted** start/end · reason ·
source (*Gantt Drag, Gantt Resize, Date Dialog, Cascade, Auto Schedule, Reconciliation, Retry, Migration, API*) · changed by / at ·
`correlation_id` (all cards moved by one user action share it) · sync status · the Work Order's dates before/after · references to the
Job Card, Work Order, Production Plan, Sales Order, operation and workstation (stored as plain values, so the history survives deleting a draft card).

Revisions are created only by the scheduling service. The controller refuses manual creation, refuses edits and refuses deletion. The single
exception is an **audited correction note** (`correct_schedule_revision`, System Manager only): it can set *corrected / correction reason /
corrected by* and nothing else — dates and facts of the revision stay as recorded — and it writes an audit-log row.

## 3. What happens on one reschedule (all or nothing)

```
API reschedule_job_card(job_card, start, end, mode, expected_modified, ...)
 1. capability + document permission; advisory validation (sequence, capacity, holidays, delivery date)
 2. warnings need confirmation (and conflicts need the Override capability); nothing is written yet
 3. ── savepoint ───────────────────────────────────────────────────────────────────────────────────
    a. SELECT ... FOR UPDATE on the Job Card(s) and the Work Order  (row locks, released on commit)
    b. optimistic concurrency: `modified` must equal the token the browser loaded, else → "stale", nothing written
    c. re-validate under the lock (the state may have moved since step 1)
    d. per card: make sure a baseline exists, edit `scheduled_time_logs`, save (ERPNext recomputes the header dates),
       READ THE DATES BACK and compare to what was asked
    e. Work Order: planned_start = earliest start, planned_end = latest end of its remaining Job Cards; READ BACK and compare
    f. append the revision row(s) and move the `ppc_revised_*` pointers
 4. any exception in (a)-(f) → ROLLBACK TO SAVEPOINT: card dates, Work Order dates and revisions are all as before
 5. after a rollback: the card is flagged "Failed", an audit row stores the *proposal* (retry status Pending)
```

* **Recursion**: PPC's own writes use flags (`ppc_in_sync`) and `update_modified=False` for its mirror fields, so saving the Job Card /
  Work Order never re-triggers a refresh or a second revision. A cascade is one transaction and one correlation id.
* **Idempotency**: asking for the schedule the card already has is a no-op (no revision). Reconciliation and retry are replays with the same
  validation, so running them twice finds nothing to do.
* **Work Order aggregation policy** (documented, tested): a Work Order's planned window is the earliest start / latest end of its Job Cards
  that are not yet Completed/Submitted/Cancelled. It is never simply set to one card's dates. Finished work never moves the window backwards.
* **Precision**: ERPNext keeps microseconds, the Gantt and the revision history keep seconds; comparisons are made at one-second precision.
  All arithmetic is on naive site-time-zone datetimes, so the browser's time zone and DST cannot shift a schedule.
* **Screen behaviour** (tested in jsdom and in a real browser): a refused or failed drag leaves nothing half-updated (bar back at its stored place,
  no drag classes or drag tooltip left); one change is saved at a time; Escape abandons a drag in progress but never a save that is on its way;
  a refresh that re-renders the chart while a drag or save is in flight keeps that bar "dragging"/"saving" and bound to the same operation.
  The state is published for tests and diagnostics as `data-ppc-drag` (`idle | dragging | pending`) on the chart and `data-ppc-busy` (loads in flight) on the panel.
* **Locked work**: started, completed, submitted or cancelled Job Cards, and cards of stopped/closed/completed Work Orders, cannot be moved,
  so they never get a revision. Actual dates are never written.

## 4. Reconciliation (Job Cards tab → buttons; API `check_schedule_consistency`, `repair_schedule_inconsistencies`, `initialize_missing_baselines`, `retry_failed_synchronizations`)

| Issue | Meaning | Automatic repair |
|---|---|---|
| `missing_baseline` | Card has a schedule but no baseline | Records one, labelled *initialized from existing schedule* |
| `missing_history` | Baseline fields exist (PPC 1.0.0) but no history rows | Adds revision 0 (and an explanatory revision if the card was moved) |
| `schedule_drift` | ERPNext schedule ≠ last accepted schedule (manual edit, import…) | Re-applies the accepted schedule **as a new revision** — only for movable cards |
| `work_order_drift` | Work Order dates ≠ aggregate of its Job Cards | Synchronises the Work Order |
| `failed_sync` | A reschedule was rolled back and not retried | Replays the stored proposal; clears a stale flag when nothing is pending |
| `revision_mismatch` | Revision numbers duplicated/missing, or history disagrees with the card | **Manual** (never rewritten) |
| `orphan_work_order` | Job Card points at a missing/cancelled Work Order | **Manual** |

Preview is the default everywhere; applying needs the *Reconcile Schedules* capability (PPC Manager by default); retry needs *Retry Failed
Operations* **and** *Reschedule*, runs with the caller's permissions and re-validates; every applied repair is audited. Completed, submitted or
started cards are reported but never changed.

## 5. Upgrading from PPC 1.0.0

`bench migrate` (a) syncs the new DocType, (b) runs the patch `backfill_schedule_history`, and (c) runs `after_migrate`, which creates the new read-only
Job Card fields. Because Frappe runs patches *before* `after_migrate`, the patch creates those fields itself first (idempotent) - this was a real defect
found only by migrating a copy of a 1.0.0 site: without it `bench migrate` aborted with `Unknown column 'ppc_schedule_revision'`. The patch gives every
Job Card that already carries a PPC 1.0.0 baseline its missing revision-0 row (and one explanatory *Migration* revision if the card has moved since). It never
touches `ppc_original_*`, never changes a schedule, never invents an author, and is safe to run repeatedly - including re-running `bench migrate` after a
failed attempt (the failed run leaves the data untouched and the patch un-logged). Cards without any baseline are left for the preview-first button.

**Rollback**: redeploying 1.0.0 leaves the new fields and the `PPC Schedule Revision` table in place but unused (nothing in 1.0.0 reads them).
*Uninstalling* the app drops the `PPC Schedule Revision` DocType and with it the history — export it first
(`bench --site <site> export-csv "PPC Schedule Revision" revisions.csv`) if you want to keep it.

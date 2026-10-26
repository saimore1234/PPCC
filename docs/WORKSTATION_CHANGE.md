# Changing a Job Card's Workstation from the Gantt chart (1.3.0)

A planner with the **Reschedule** capability can move a Job Card to another ERPNext **Workstation** without opening the
Job Card form. The standard `Workstation` DocType is used as it is - no machine master was added.

## How to use it

| You want to | Do this |
|---|---|
| Move an operation to another machine | On **Production Gantt** (grouped by *Workstation*, the default) drag the bar **down or up onto another machine's row**. The target row lights up while you drag. |
| Move it to a later/earlier time **and** another machine | Drag diagonally - one gesture, one confirmation, one history entry. |
| Reach a machine that has nothing in view | Tick **Show all machines** (above the chart): every Workstation becomes a row, empty ones included. |
| Use the keyboard / avoid dragging | Click the bar, then **Change Workstation** in the detail panel. |
| See which machine a bar is on | The machine name is on every bar (`Operation · Workstation`). |

Whatever you do, nothing is saved until you confirm, and the screen only changes after the **server** has saved and
re-read it. A refused or failed change puts the bar back exactly where it was and tells you why.

### If the machine is free
A confirmation shows the current and the proposed Workstation, the dates (unchanged, or old → new), the machine's load in that
period, compatibility information, and any warnings. An optional reason is written to the history.

### If the machine is busy - *Workstation Conflict Detected*
The popup names the blocking Job Card, its operation, Work Order, status and the interval it occupies, and offers:

1. **Cancel Change** - nothing happens; the bar returns.
2. **Choose Another Machine** - searchable list of every Workstation with *available / busy / current*, what is in the way,
   load %, the next free slot, and compatibility. *Select* uses your interval; *Use next slot* also takes that machine's next free slot.
3. **Find Next Available Slot** - computes the next slot of the same length on this machine (not before the preceding operation
   ends, skipping the machine's holidays), previews it, and only uses it if you accept.
4. **Reschedule Conflicting Operation** - offered **only** when it is safe: the machine runs one operation at a time, at most
   5 operations are in the way, each is a movable draft with no recorded time and you may write to it, and each pushed operation
   (with the operations that depend on it) fits where it would land. A preview table shows every move before you accept.
5. **Override Conflict** - only for roles holding **Override Conflicts** (PPC Manager by default). A reason of at least 10
   characters is required and is stored with the revision and in the audit log.

Other things that need an override are listed in the same popup: starting before the preceding operation ends, and a machine
whose **Workstation Type differs from the one the operation requires**.

## What the server checks (and rechecks while saving)

* **Eligibility.** Only a draft Job Card that is *Open*, *Material Transferred* or *On Hold* **with no recorded time** can change
  machine. *Work In Progress* and anything with time logs is refused (ERPNext books actual time against the card's Workstation);
  completed, submitted and cancelled cards never; neither does a card of a stopped/closed/completed Work Order.
* **Occupancy** is read from the Job Cards' real time rows (`Job Card Scheduled Time` for drafts, `Job Card Time Log` for
  recorded time; an open log runs until now; header dates only when a card has no rows at all). Cancelled cards occupy nothing;
  Draft, Open, On Hold and Work In Progress cards are real bookings. The card being moved never conflicts with itself.
  Intervals are half-open: an operation that starts exactly when another ends is not a conflict.
* **Capacity.** A machine with `production_capacity` N accepts a new operation unless N others are running at the same instant.
* **Dependencies.** Same rules and previews as for a date change, including *shift following operations* (previewed first).
* **Compatibility** is formal only: the operation's required *Workstation Type* (from the Job Card, else the Work Order Operation)
  against the machine's type. With no type configured the dialog says so; compatibility is never guessed from names.
* **Workstation `status`** (Off / Problem / Maintenance) produces a warning that needs confirmation. ERPNext's Workstation has no
  *disabled* flag, so no machine is hidden.
* **Working hours / holidays**: a start or end outside working hours, or on a holiday of the target machine, is a warning.

### Atomic save and concurrency
`change_job_card_workstation` (POST) runs in one savepoint: it locks the **Workstation rows** (source and target, always in the
same order), then the Job Card and its Work Order; rejects a stale screen (`expected_modified`); runs the full check again; writes the
Job Card through its normal document lifecycle (`save()`, no SQL shortcuts, no skipped validation); reads the Workstation and dates
back and verifies them; re-derives the Work Order dates; verifies those; and only then appends the revision.
Any failure rolls everything back and leaves the previous accepted schedule intact (the card is flagged *Failed* so the retry tools find it).

Two planners racing for the same slot are serialised by the machine's row lock; the second one **re-reads the first one's committed
booking** (the request starts a fresh `READ COMMITTED` transaction - MariaDB's default would hide it) and receives a conflict.
A request id (`correlation_id`) makes a repeated request idempotent: it is applied once.

### History
Every accepted machine change appends **exactly one** `PPC Schedule Revision` (source *Workstation Change*) with: previous and new
Workstation, previous and new dates, reason, user, time, the Work Order's before/after dates, **conflict details** (JSON of what was in
the way), **override used** and the **override reason**. Revisions stay append-only; the original baseline is never touched.
A refused override attempt is recorded as an audit-log *warning*, a failed save as an audit-log *failure*; neither is a revision.
Pushed operations get their own *Cascade* revisions. The machine change shows in the detail panel's history, in **Schedule History**
(filter *Source = Workstation Change*) and in the Gantt bar and tooltips.

## API

| Method | Use |
|---|---|
| `check_workstation_change(job_card, workstation, start?, end?, shift_dependents?)` | Advisory check; changes nothing. |
| `get_workstation_candidates(job_card, start?, end?, search?, limit?)` | Machines with availability, conflicts, load, next slot, compatibility. |
| `find_next_workstation_slot(job_card, workstation, start?, end?)` | Next feasible slot. |
| `change_job_card_workstation(job_card, workstation, start?, end?, reason?, expected_modified?, confirmed, shift_dependents?, override_conflicts?, override_reason?, reschedule_conflicts?, correlation_id?)` | The atomic save (POST). Returns `ok`, or `conflict` / `needs_confirmation` / `stale` / `errors`. |

All need the **Reschedule** capability and write permission on the Job Card; the override additionally needs **Override Conflicts**.

## Staging test instructions

1. On a **staging copy** (never production): deploy the branch, `bench --site <staging> migrate` (adds the revision fields and the audit action), `bench build --app ppc_production_control`, hard-reload.
2. Create a plan with a BOM that has two operations on two machines, submit it, submit its Work Order.
3. **Production Gantt** ▸ filter by the plan ▸ drag the second operation onto a free machine ▸ confirm ▸ reload: it stays. Open the bar's detail panel: the history shows `Machine A → Machine B`.
4. Book the target machine with another Job Card over the same time, drag again: the conflict popup appears; try each button.
5. Log in as a *Production Planner* (no override): the *Override Conflict* button is absent.
6. Automated: `docs/VALIDATION_CHECKLIST.md` lists the commands and results.

## Limitations

* Occupancy is judged at whole-second precision (ERPNext keeps microseconds).
* A contiguous slot is proposed; the machine's shift pattern is only reported as a warning, not used to cut the slot into pieces.
* *Reschedule Conflicting Operation* is limited to one-at-a-time machines and at most 5 operations.
* The Work Order Operation row keeps the BOM's planned machine; ERPNext lets the Job Card differ.
* Only machines the user may read are listed; document permissions are enforced by ERPNext on the save.

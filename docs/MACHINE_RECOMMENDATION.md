# Best-machine recommendation on the Sales Order (1.5.0)

A planner presses **Find Best Machine** on a Sales Order line and sees which Workstations can make the item, ranked, with the earliest start,
the estimated completion and the delivery risk. The chosen machine is stored on the line, travels with it into the Production Plan, and is used
when the Work Order (and so its Job Cards) is created - after being checked again, because nothing is reserved.

> **A recommendation is a planning aid, not a booking.** The PPC scheduling engine and ERPNext validate the real schedule when Work Orders and
> Job Cards are created. The dialog says so, and so does the form.

## What it reads (and what it never guesses)

| Needed | Taken from |
|---|---|
| Valid BOM | the line's BOM, else the item's default active BOM, else (with a note) its newest active BOM |
| Operations, sequence, durations | the BOM's operations: `time_in_mins`, `fixed_time`, `batch_size`, BOM quantity, `sequence_id` - scaled exactly as the Work Order scales them (`time / BOM qty x qty / batch size`; a fixed-time operation is not scaled) |
| Which Workstations may do an operation | the **union** of (1) every Workstation of the BOM operation's *Workstation Type*, (2) the Workstations listed on the *Operation* in **PPC Compatible Workstations**, (3) the BOM operation's own Workstation. A row switched off in that list removes the machine. |
| Capacity, working hours, holidays | the Workstation (production capacity, working hours, holiday list) and *Manufacturing Settings* (overtime, production on holidays, minutes between operations) |
| What is already booked | the Job Cards' real time rows (`services/machines.py`: planned rows of draft cards, actual rows of started cards); optionally draft Work Orders (setting off by default) |
| Promised date | the line's delivery date, else the Sales Order's |

A machine is **never** treated as compatible because of its name or because it is free. With no Workstation Type, no operation list and no BOM
machine, the operation gets *no* candidates and a warning that says what to configure. A duration that is missing from the BOM is shown as
unknown: no start, end or completion is made up for it. Workstations that are Off, in Maintenance or with a Problem are not offered (the reason
is listed). Efficiency and cost are used only when **every** candidate has the data (an efficiency % on the Operation's list, an hourly rate on the
Workstation); otherwise that factor is dropped and the remaining weights are renormalised.

## Ranking

Each candidate gets a score from 0 to 100 from the factors for which there is data, weighted by **PPC Settings -> Machine Recommendation**:

| Factor (default weight) | Meaning |
|---|---|
| Earliest completion (45) | the machine that finishes the operation sooner scores higher |
| Lower workload (25) | the share of capacity already booked in the *Workload Window* (default 7 days) |
| Meets delivery date (20) | projected finish of the whole route against the delivery date; only with a delivery date and known durations |
| Lower cost (5) / Efficiency (5) | only when every candidate has the data |

Weights never override compatibility: an incompatible machine is never in the list, whatever the weights. A weight of 0 switches a factor off.
*Look-ahead* (default 120 days) bounds the search for a free slot; *Unavailable After* (default 24 h) is how much later than planned a chosen
machine may be free before it is treated as unavailable at Work Order creation; *One Machine for All Operations* limits the choice to machines
compatible with every operation.

Timing follows ERPNext's capacity planning: the time is laid into the machine's working-hour slots and skips its holidays (unless overtime or
production on holidays is allowed); a machine with capacity *n* can run *n* operations at once; operations of a BOM run one after the other (with
the *minutes between operations* gap), operations that share a non-zero sequence id start together. Operations placed earlier in the same
recommendation count as bookings for the later ones, so parallel operations do not double-book a machine.

**Estimated completion** is the end of the last operation of the feasible schedule. It is kept apart from the promised delivery date and from the
actual completion; when it falls after the delivery date the dialog says so ("Misses delivery date", with the days).

## On the Sales Order line

Section **Machine Recommendation** on *Sales Order Item*: **Find Best Machine** (button), Machine Preference (*Recommended / Manual / No
Preference*), Selected / Preferred Workstation, Estimated Production Start / Completion, Recommendation Status (*Not Calculated, Recommended,
Selected, Stale, Incomplete Data, Unavailable*) and Recommendation Updated At. The fields can be changed after the order is submitted.

* **The server decides what is saved.** When the preference is new or changed, the estimates and the status are recalculated on save from the
  stored selection - values sent by the browser are replaced. A machine that cannot do any operation of the item's BOM is refused with an error
  that no role can override.
* **Stale.** If the item, quantity, UOM, BOM or delivery date changes after the recommendation was made, the choice is kept but marked *Stale*
  (the form shows a banner until it is recalculated).
* **Unavailable.** An hourly job marks lines whose preferred machine has since gone Off / Problem / Maintenance and notifies the order's owner.
  Nothing is changed silently - press *Find Best Machine* again.

## Hand-over

1. **Production Plan.** When the plan is saved, each item made from a Sales Order line (`sales_order_item`) receives that line's preferred
   Workstation, preference and per-operation machine plan. The source line stays referenced.
2. **Work Order.** Before a Work Order is inserted, the machines are applied to its operations - after checking *now* that each is still
   compatible, not Off / Problem / Maintenance, and has a free slot within the tolerance. The Job Cards ERPNext creates on submit then land on
   those machines, and the existing PPC scheduler and Gantt work from there.
3. **If a machine is no longer usable** the BOM's own machine stays, the Work Order's *Machine Plan (PPC)* note says which and why, a PPC Audit
   Log row (*Apply Machine Plan*, Warning) records it with the ranked alternatives, and the production planner gets a notification. PPC never
   submits Work Orders, completes Job Cards or creates stock transactions because of a recommendation.

## Permissions

Anyone who may create or edit Sales Orders, or holds the PPC **View** capability, may ask for a recommendation (it needs read access to the item,
and to the order when one is named). The answer contains schedule aggregates only, never other users' documents. Saving a preference needs the
normal Sales Order write permission.

## Configuration checklist

1. Give every BOM operation a **Workstation Type** (and make sure the Workstations carry that type), or list the machines on the Operation under
   **PPC Compatible Workstations**.
2. Set each Workstation's **production capacity**, working hours and holiday list.
3. Review **PPC Settings -> Machine Recommendation**.

## API

`ppc_production_control.api.recommend_machines(item_code, qty, uom, bom_no, delivery_date, forced, sales_order)` (GET/POST). `forced` is a JSON
object `{operation: workstation}` for the planner's own choice. Returns `operations[]` (each with ranked `candidates[]`, `excluded[]`,
`warnings[]`), `overall` (start, completion, delivery risk), `warnings`, `data_gaps`, `status`, `weights`, `disclaimer`.

## Tests

`tests/test_recommendation.py` (64): duration scaling, grouping, scoring, working-hour layout and slot planning (pure); compatibility from the
Workstation Type / the Operation list / the BOM, never from a name; missing BOM / operations / duration / capacity; several operations in
sequence; different quantities; existing bookings and capacity; ranking and explanations; delivery risk; single-machine mode; the planner's own
choice; saving on the right line with server-side estimates; stale marking; propagation to the Production Plan and the Work Order; unavailable /
busy / incompatible machines at Work Order creation (note, notification, audit); the hourly check; permissions.
`tests/e2e/ppc_recommend_e2e.js`: the dialog in a real browser, from the form to the Job Card.

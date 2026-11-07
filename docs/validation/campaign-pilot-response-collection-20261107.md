# Fixed-plan collection after the pilot response failure

## Incident

On 2026-11-07 the completed, archived pilot trio passed numerical checks but
failed the existing response window: rear displacement was approximately 7–12 mm,
and each pilot reported rear skin deletion. The existing 1–3 mm, intact-rear
acceptance condition remains unchanged. The controller moved to
`needs_investigation` and stopped relaying, so free Actions slots stopped refilling.

The human had explicitly requested all 400 fixed-input FE samples using the
historical Actions reference mesh. Provisional collection was already enabled;
its dispatch and relay phases did not handle this particular scientific failure.

## Repair

An audited `provisional_collection.after_pilot_response_failure` decision permits
continued raw collection of the exact initial 400 designs. A digest binds their
IDs and physics hashes. Entry requires the full execution scope, the selected
reference mesh, the archived numerically passing pilot trio, and this specific
failed response window. Other investigation errors retain their existing hold.

The new collection phase admits only development/test configurations. Existing
model, reference mesh, source/runtime, infrastructure, material, restart, archive,
ticket, numerical and continuation checks still apply. No pilot acceptance test,
physical input or native solver component changes in this repair.

The pilot failure is retained as `scientific_block` in metadata and status.
`protocol_error` exposes top-level operational failures separately from row errors.
Global qualification and ML eligibility remain false. Completion of 400 actual,
archived, numerically passing initial configurations ends collection with
`collection_completed` and disables dispatch; it does not trigger ML analysis or
claim scientific acceptance. Experimental fracture calibration remains absent.

## Verification

- Before repair: the regression suite reproduced the missing collection phase,
  absent relay path and blocked-row handling (5 failures).
- After repair: 436 software tests passed, 1 skipped; changed-file Ruff and ty passed.
- Regressions cover exact-plan authorization, unknown errors, native proof drift,
  input drift, missing pilot archive/quality, frozen-flag bypass, quarantine,
  export eligibility, relay and completion after 400 archives.
- Operational replay and deployment receipts are saved outside the checkout in
  `C:/UET/DO_AN/output/core-damage-v2-20261107/response-window-collection-*.json`.

These software test counts are not FE sample counts.

## Transient dispatch interruption during deployment

The first refill encountered an actual GitHub HTTP 500 on a worker dispatch for
`dzdeptrai390`. The central reservation and repository ticket were already durable.
Other owners completed their refill, but propagation of that exception prevented
the controller from publishing its final status and continuing to its relay step.

Refill now retains uncertain reservations, records a compact warning in quota
observations, and continues its bounded owner slots. An uncertain redispatch also
keeps the same ticket and permits reconciliation to continue. Permanent errors
still surface. There is no immediate blind POST retry or new solver ticket. The
existing CAS claim and workflow lookup continue to prevent duplicate solvers.
The regression first reproduced 5 failures for HTTP 500/502/503/504 and a transport
timeout. Targeted campaign tests and Ruff/ty passed after the fix.

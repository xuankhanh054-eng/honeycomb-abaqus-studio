# Separate 6.5 m/s collection

On 2026-11-07 the human requested stopping every running campaign job and
restarting the 400 configurations at 6.5 m/s. The separate campaign namespace is
`core-damage-v2-20261107-v6p5`. Previous 8 m/s archives and receipts remain under
their original namespace. No previous result or checkpoint counts as a new sample.

The new initial designs retain the same parameter points, owner allocation,
historical Actions reference mesh, material, mass 2.65 kg, duration 8 ms, four
CPUs, and output intervals. Only the velocity input changes; the corresponding
initial kinetic energy is 55.98125 J. All model and execution hashes are rebuilt.

The `fixed_collection` phase requires an explicit human decision tied to the
400 IDs and physics hashes. Admission still checks the full reference model,
infrastructure, material/restart/archive evidence, runtime and immutable tickets.
Unexpected execution failures retain the investigation hold. Four hundred actual,
archived, numerically passing results end collection without launching ML.

Fresh workers need the updated namespace parser. A separate metadata upgrade
proof records the newer worker revision and byte identities of unchanged native
components against the qualified geometry revision. Continuations keep their
predecessor revision. The earlier STA recovery status parser repair is recorded
separately; it does not change model generation or native solver invocation.

Physical calibration and mesh/time convergence at 6.5 m/s remain unestablished.
Software tests and inherited native infrastructure evidence do not establish them.
Objective maxima and tail stability must be assessed from the new actual histories.

Operational cancellation, manifest comparison, source identity, publication and
dispatch receipts are saved in the two campaign-specific output directories.

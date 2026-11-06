# Campaign storage admission and native checkpoints

The initial 400 configurations have not run yet. Pilot runs are separate. The
fixed load remains H20, sphere D13.1 mm / 2.65 kg, 8 m/s (84.8 J), 8 ms,
four CPUs, double precision, and no mass scaling.

## Measured storage probes

Source: `0538e4acc0e8f151fadd77b9e5728dcc972556e8`. These short 30-microsecond
probes used the full spatial mesh and field list, with two field intervals.
Their incomplete-event review status is expected. They are excluded from the
400 samples. Extrapolation is a storage estimate, not physical validation.

| Mesh factor | Workflow runs | 3-frame ODB, GB | Estimated 801-frame ODB, GB | Estimated 1201-frame ODB, GB |
| --- | --- | ---: | ---: | ---: |
| 1 | 42261840612 | 0.1123 | 23.112 | 34.641 |
| .75 | 42261841899 / 42261840630 | 0.1936 | 40.141 | 60.165 |
| .5 | 42261841373 / 42261840986 | 0.4114–0.4120 | 85.749 | 128.525 |

The estimate subtracts the datacheck ODB (frame zero) from the three-frame
ODB, then scales the remaining two field intervals. The existing cold-start
ODB predictor exceeds these estimates. Native state in the largest probes
is bounded by the additional mesh-dependent allowance below.

## Failure and repair

The former admission rule reserves 3.5 times the predicted ODB plus a separate
20-GB safety reserve. It denied the fine mesh on a 155.9-GB hosted filesystem.
The former checkpoint tar also duplicated the ODB during packaging and restore.

For Explicit continuation with full-field export disabled, admission now uses:

`predicted ODB GB + max(4, 2 * mesh complexity) GB + one 1-GiB transfer part`

The 20-GB safety reserve remains separate. Full-field exports retain the old
rule. The .5-mesh eight-ms reference requires 135.16 GB including the reserve;
insufficient capacity still rejects preparation. Continuations credit only
already restored native job files and still require the safety reserve and
space for at least one transfer part.

Directory checkpoints seal every file's SHA-256 and byte length, hard-link the
original payloads, and publish arbitrary native file suffixes only through the
explicit inventory. Restore checks every file before creating the destination
and hard-links verified bytes. Legacy tar checkpoints remain readable.

Disposable SDK reclamation is restricted to ephemeral GitHub-hosted Linux
runners and preserves the active Python, uv, Node, Docker, and source paths.

Execution receipts retain the actual source revision. A continuation stays on
its predecessor's revision. CSV energy is resolved from the velocity load, so
it records 84.8 J rather than the inactive seven-J default. Each new source
requires its own native cross-runner qualification; old evidence is not relabeled.

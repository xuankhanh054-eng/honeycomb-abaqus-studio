"""Reproducible core-damage campaign: scientific inputs and fail-closed admission gates."""
from __future__ import annotations

import math
from collections import Counter

import numpy as np
from scipy.stats import qmc

from .models import BatchSpec, JohnsonCook, Material, ModelSpec, digest

VERSION = "core-damage-v2"
SEED = 20261106
INITIAL_DURATION_S = .008
DEFAULT_CAMPAIGN_ID = VERSION + "-20261106"
OFAT_LEVELS = 15
DEVELOPMENT_LHS_COUNT = 259
OWNERS = ("luukhanh24", "KhanhStark", "xuankhanh24", "dzdeptrai390",
          "khanhstarkpubg-ux", "xuankhanh054-eng", "starkdzdz-eng", "luukhank574-design")
BOUNDS = {"reentrant_delta_deg": (25., 45.), "foil": (.035, .115),
          "cell_size": (4.5, 8.2), "reentrant_cell_height": (3., 7.)}
BASE = {"reentrant_delta_deg": 35., "foil": .0635, "cell_size": 6.35, "reentrant_cell_height": 5.}
NUMERICAL_PARAMETERS = frozenset((
    'duration', 'cpus', 'precision', 'mass_scaling', 'bulk_linear', 'bulk_quadratic',
    'field_intervals', 'history_intervals', 'field_variables', 'history_variables',
    'mesh_preset', 'core_centre_seed', 'core_outer_seed', 'skin_centre_seed', 'skin_outer_seed',
    'reentrant_depth_seed', 'core_z_divisions', 'ball_seed', 'fine_radius', 'skin_patch_half',
    'shell_element', 'shell_points', 'min_segment_length', 'tie_tolerance'))


def reference_model(**updates) -> ModelSpec:
    material = Material(name="AA6061-T6-reference", hardening="johnson_cook",
                        johnson_cook=JohnsonCook(), yield_stress=324, ultimate=324)
    values = dict(physics_version=VERSION, core_topology="reentrant_0", reentrant_geometry="angle",
                  core_height=20, skin=.75, bottom_skin=.75, clamp_mode="perimeter",
                  core_material=material.model_dump(), face_material=material.model_dump(),
                  impactor_shape="sphere", mass_kg=2.65,
                  ball_size_mode="diameter", ball_diameter=13.1, ball_radius=6.55,
                  loading="velocity", velocity_m_s=8,
                  friction=.2, gravity=0, gap=.1, adiabatic=True, initial_temperature_C=20,
                  cpus=4, mesh_preset="custom", fine_radius=22, skin_patch_half=12,
                  skin_self_contact=True, duration=INITIAL_DURATION_S, field_intervals=800, history_intervals=4000,
                  field_variables=["U", "V", "S", "PEEQ", "STATUS", "SDEG", "DMICRT", "TEMP", "CFORCE"],
                  history_variables=["ALLAE", "ALLIE", "ALLKE", "ALLPD", "ALLDMD", "ALLSE", "ALLVD",
                                     "ALLFD", "ALLWK", "ETOTAL", "ALLIHE"], **BASE)
    values.update(updates)
    return ModelSpec.model_validate(values)


def geometry_valid(values):
    try:
        model = reference_model(**values)
    except ValueError:
        return False
    throat = model.cell_size - 2 * model.effective_indent()
    return throat > max(8 * model.foil, .5)


def ofat_values(low, high, baseline, count=OFAT_LEVELS):
    """Return endpoint-inclusive OFAT levels without duplicating the reference."""
    lower_count = count // 2 + 1
    upper_count = count - lower_count
    lower = np.linspace(low, baseline, lower_count, endpoint=False)
    upper = np.linspace(baseline, high, upper_count + 1, endpoint=True)[1:]
    values = [float(value) for value in (*lower, *upper)]
    if len(values) != count or any(math.isclose(value, baseline, rel_tol=0, abs_tol=1e-12) for value in values):
        raise ValueError("OFAT levels must exclude the reference value")
    return values


def model_hashes(model):
    resolved = model.resolved()
    return {'physics_hash': digest(resolved),
            'physical_inputs_hash': digest({k: v for k, v in resolved.items() if k not in NUMERICAL_PARAMETERS}),
            'numerical_inputs_hash': digest({k: v for k, v in resolved.items() if k in NUMERICAL_PARAMETERS})}


def record(model, role, index, owner, varied):
    # Validate defaults as explicit values before freezing JSON identities. Otherwise
    # e.g. a float default20 is serialized as20, then a worker parses it as20.0.
    model = ModelSpec.model_validate(model.model_dump())
    return {"id": f"{role}-{index:04d}", "role": role, "owner": owner,
            "repository": owner + "/honeycomb-abaqus-studio", "varied_parameters": varied,
            **model_hashes(model), "model": model.model_dump(),
            "status": "pending", "attempt": 0, "segment": 0}


def pilots(velocities=(8.,)):
    rows = []
    for velocity in velocities:
        for foil in (.035, .0635, .115):
            model = reference_model(velocity_m_s=velocity, foil=foil)
            i = len(rows)
            rows.append(record(model, "pilot", i + 1, OWNERS[i % 8], ["foil"]))
    return rows


def campaign_manifest(campaign_id: str | None = None):
    """Production geometry is proposed, never runnable until independent pilot gates pass."""
    dev = [(BASE.copy(), [])]
    for name, (low, high) in BOUNDS.items():
        for value in ofat_values(low, high, BASE[name]):
            point = {**BASE, name: float(value)}
            if not geometry_valid(point):
                raise ValueError("OFAT design is not manufacturable: " + name)
            dev.append((point, [name]))
    seen = {digest(point) for point, _ in dev}

    def lhs(count, seed):
        # Conditional LHS: preserve the explicit valid-domain rejection in the manifest.
        sampler = qmc.LatinHypercube(d=4, seed=seed)
        accepted = []
        for _ in range(100):
            low, high = np.array(list(BOUNDS.values())).T
            values = qmc.scale(sampler.random(count - len(accepted)), low, high)
            for vector in values:
                point = dict(zip(BOUNDS, map(float, vector), strict=True))
                key = digest(point)
                if key not in seen and geometry_valid(point):
                    seen.add(key)
                    accepted.append((point, list(BOUNDS)))
            if len(accepted) == count:
                return accepted
        raise ValueError("Unable to fill valid conditional LHS")

    dev += lhs(DEVELOPMENT_LHS_COUNT, SEED)
    test = lhs(80, SEED + 1)
    rows = []
    for role, points in (("development", dev), ("test", test)):
        for i, (point, varied) in enumerate(points):
            rows.append(record(reference_model(**point), role, i + 1, OWNERS[i % 8], varied))
    return {"schema_version": 1, "id": campaign_id or DEFAULT_CAMPAIGN_ID, "physics_version": VERSION,
            "seed": SEED, "source_sha": None, "frozen": False,
            "material_validation": "reference_unvalidated", "bounds": BOUNDS,
            "domain": "cell_size - h*tan(delta) > max(8*foil, 0.5 mm)",
            "gates": {}, "license_capacity": 0, "capacity_stage": 8,
            "max_adaptive": 80, "confirmation_count": 20, "execution_scope": "pilot",
            "pilot_designs": pilots(), "designs": rows}


def one_run_spec(design):
    return BatchSpec(name=design["id"], project_id=VERSION, model=design["model"], max_runs=1,
                     data_profile="research", render_images=False, backup_after_run=False,
                     export_all_fields=False, runtime_budget_minutes=240).model_dump()


def numerical_gate(summary):
    errors = []
    for key in ("solver_completed", "history_integrity_passed", "damage_fields_complete"):
        if summary.get(key) is not True:
            errors.append(key)
    for key, ceiling in (("maximum_energy_balance_drift_percent", 5),
                         ("maximum_artificial_energy_ratio_developed_percent", 5),
                         ("contact_force_check_max_relative_percent", 2)):
        value = summary.get(key)
        if value is None or not math.isfinite(value) or value > ceiling:
            errors.append(key)
    expected = summary.get("expected_energy_J")
    initial = summary.get("initial_kinetic_energy_J")
    if expected is None or initial is None or not math.isclose(expected, initial, rel_tol=.001):
        errors.append("initial_kinetic_energy")
    return {"passed": not errors, "errors": errors,
            "rear_intact": summary.get("rear_skin_deleted") is False,
            "physical_validation": "not_established"}


def response_converged(short, long, tolerance=.02):
    if not all(numerical_gate(s)["passed"] for s in (short, long)):
        return False
    if short.get("rear_skin_deleted") != long.get("rear_skin_deleted"):
        return False
    for name, floor in (("max_rear_downward_mm", .02), ("SEA_core_J_kg", 1.e-6)):
        a, b = short.get(name), long.get(name)
        if a is None or b is None or abs(a - b) > tolerance * max(abs(b), floor):
            return False
    return short.get("event_finished") is True


def choose_velocity(results):
    eligible = []
    for velocity in sorted({r["velocity_m_s"] for r in results}):
        trio = [r for r in results if r["velocity_m_s"] == velocity]
        if len(trio) != 3 or not all(numerical_gate(r)["passed"] for r in trio):
            continue
        trio.sort(key=lambda r: r["foil"])
        baseline = trio[1]
        w = baseline.get("max_rear_downward_mm")
        if w is None or baseline.get("rear_skin_deleted") or not 1 <= w <= 3:
            continue
        deflections = [r.get("max_rear_downward_mm") for r in trio]
        if any(v is None for v in deflections) or max(deflections) - min(deflections) <= max(.05, .05 * w):
            continue
        eligible.append((abs(w - 2), velocity))
    return min(eligible)[1] if eligible else None


def freeze(manifest, velocity, duration, mesh_factor, evidence):
    required = ("material", "mesh", "time", "damage_sensitivity", "archive_restore")
    if any(evidence.get(k, {}).get("passed") is not True for k in required):
        raise ValueError("Pilot gates are not complete")
    if velocity != 8. or duration != INITIAL_DURATION_S:
        raise ValueError("The fixed campaign requires 8 m/s and 8 ms")
    if mesh_factor not in (1., .75, .5):
        raise ValueError("Unapproved mesh factor")
    if manifest.get("frozen") or not manifest.get("source_sha"):
        raise ValueError("Freeze requires source SHA and an unfrozen campaign")
    for row in manifest["designs"]:
        if row['role'] not in ('development', 'test'):
            continue
        model = reference_model(**{k: row["model"][k] for k in BOUNDS},
                                velocity_m_s=velocity, duration=duration,
                                field_intervals=round(duration / .00001),
                                history_intervals=round(duration / .000002))
        for key in ("core_centre_seed", "core_outer_seed", "skin_centre_seed", "skin_outer_seed", "reentrant_depth_seed"):
            setattr(model, key, getattr(model, key) * mesh_factor)
        model = ModelSpec.model_validate(model.model_dump())
        if row['status'] != 'pending' and model_hashes(model)['physics_hash'] != row['physics_hash']:
            raise ValueError('Cannot change inputs of an already dispatched configuration during freeze')
        row.update(model=model.model_dump(), **model_hashes(model))
    manifest.update(frozen=True, velocity_m_s=velocity, duration_s=duration,
                    mesh_factor=mesh_factor, gates=evidence)
    manifest["protocol_hash"] = digest({k: manifest[k] for k in
                                      ("source_sha", "physics_version", "velocity_m_s", "duration_s", "mesh_factor", "gates")})
    return manifest


def progress(manifest):
    samples = [r for r in manifest['designs'] if r['role'] in ('development', 'adaptive', 'test', 'confirmation')]
    counts = Counter(r["status"] for r in samples)
    accepted = sum(r.get("archive_verified", False) and r.get("quality", {}).get("passed", False)
                   for r in samples)
    return {"campaign_id": manifest["id"], "frozen": manifest["frozen"], "counts": dict(counts),
            "initial_configurations": 400, "configuration_count": len(samples),
            "audited_configurations": accepted, "audited_percent": 100 * accepted / max(1, len(samples)),
            "qualification_counts": dict(Counter(r['status'] for r in manifest['designs'] if r['role'] == 'qualification')),
            "pilot_counts": dict(Counter(r["status"] for r in manifest["pilot_designs"])),
            "license_capacity": manifest["license_capacity"], "capacity_stage": manifest["capacity_stage"]}

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from honeycomb_studio.campaign import (
    DEVELOPMENT_LHS_COUNT,
    OFAT_LEVELS,
    campaign_manifest,
    freeze,
    geometry_valid,
    model_hashes,
    ofat_values,
    one_run_spec,
    pilots,
    reference_model,
)
from honeycomb_studio.models import BatchSpec, ModelSpec, digest


@pytest.mark.parametrize('frozen', [False, True])
def test_campaign_identity_survives_json_and_worker_model_validation(frozen):
    manifest = campaign_manifest()
    if frozen:
        manifest['source_sha'] = 'a' * 40
        gates = {k: {'passed': True} for k in ('material', 'mesh', 'time', 'damage_sensitivity', 'archive_restore')}
        manifest = freeze(manifest, 8., .008, .75, gates)
    for design in manifest['pilot_designs'] + manifest['designs']:
        transported = json.loads(json.dumps(design))
        worker_model = BatchSpec.model_validate(one_run_spec(transported)).model
        assert worker_model.core_height == 20, design['id']
        assert digest(transported['model']) == digest(worker_model.model_dump()), design['id']
        assert transported['physics_hash'] == digest(worker_model.resolved()), design['id']


def test_reference_contract_units_and_identity():
    model = reference_model()
    spec = model.resolved()
    assert spec["impact_mass"] == pytest.approx(.00265)
    assert spec["impactor_shape"] == "sphere"
    assert spec["ball_diameter"] == pytest.approx(13.1)
    assert spec["ball_radius"] == pytest.approx(6.55)
    assert spec["velocity"] == 8000
    assert spec["energy_J"] == pytest.approx(84.8)
    assert spec["duration"] == .008
    assert spec["field_intervals"] == 800
    assert spec["history_intervals"] == 4000
    assert spec["core_height"] == 20
    assert spec['fine_radius'] == 22 and spec['skin_patch_half'] == 12
    assert spec["core_material"]["johnson_cook"]["d3"] == .47
    assert digest(spec) != digest(reference_model(velocity_m_s=10).resolved())
    BatchSpec.model_validate(one_run_spec(pilots()[0]))
    with pytest.raises(ValueError):
        reference_model(core_material={**model.core_material.model_dump(), "johnson_cook": {"d3": -47}})
    assert ModelSpec().impactor_shape == "sphere"
    before = model_hashes(model)
    mesh = model_hashes(reference_model(core_centre_seed=.35))
    foil = model_hashes(reference_model(foil=.05))
    assert before['physical_inputs_hash'] == mesh['physical_inputs_hash']
    assert before['numerical_inputs_hash'] != mesh['numerical_inputs_hash']
    assert before['physical_inputs_hash'] != foil['physical_inputs_hash']
    assert before['numerical_inputs_hash'] == foil['numerical_inputs_hash']
    assert before['physics_hash'] != mesh['physics_hash']


def test_export_records_resolved_energy_and_actual_worker_source(tmp_path):
    import csv

    from honeycomb_studio.campaign_export import export_campaign

    manifest = campaign_manifest()
    manifest['source_sha'] = 'a' * 40
    row = manifest['designs'][0]
    row['source_sha'] = 'b' * 40
    manifest.update(designs=[row], pilot_designs=[])
    export_campaign(manifest, tmp_path)
    with (tmp_path / 'samples.csv').open(encoding='utf-8-sig', newline='') as stream:
        actual = list(csv.DictReader(stream))[0]
    assert float(actual['input.energy_J']) == pytest.approx(84.8)
    assert actual['source_sha'] == row['source_sha']


def test_manifest_reproducible_valid_partition_and_fail_closed():
    campaign = campaign_manifest()
    assert campaign == campaign_manifest()
    assert len(campaign["designs"]) == len({r["physics_hash"] for r in campaign["designs"]}) == 400
    for owner in {r["owner"] for r in campaign["designs"]}:
        assigned = [r for r in campaign["designs"] if r["owner"] == owner]
        assert len(assigned) == 50
        assert sum(r["role"] == "test" for r in assigned) == 10
    assert all(geometry_valid({k: r["model"][k] for k in campaign["bounds"]}) for r in campaign["designs"])
    for name, (low, high) in campaign["bounds"].items():
        values = ofat_values(low, high, campaign["designs"][0]["model"][name])
        assert len(values) == OFAT_LEVELS and low in values and high in values
        assert campaign["designs"][0]["model"][name] not in values
    assert sum(r["role"] == "development" and r["varied_parameters"] == [] for r in campaign["designs"]) == 1
    assert sum(r["role"] == "development" and len(r["varied_parameters"]) == 1 for r in campaign["designs"]) == 4 * OFAT_LEVELS
    assert sum(r["role"] == "development" and len(r["varied_parameters"]) == 4 for r in campaign["designs"]) == DEVELOPMENT_LHS_COUNT
    assert all(r["varied_parameters"] == ["foil"] for r in campaign["pilot_designs"])
    assert campaign_manifest("core-damage-v2-20261107")["id"] == "core-damage-v2-20261107"
    with pytest.raises(ValueError, match="gates"):
        freeze(copy.deepcopy(campaign), 15, .004, 1, {})


def test_solver_material_deck_is_explicit_and_unit_correct(tmp_path):
    path = Path(__file__).parents[1] / "src/honeycomb_studio/adapters/damage_keywords.py"
    module_spec = importlib.util.spec_from_file_location("damage_keywords", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    deck = tmp_path / "test.inp"
    deck.write_text("*Shell Section, elset=CORE, material=REF\n.0635,5\n*Material, name=REF\n"
                    "*Density\n2.7e-9\n*Elastic\n70000,.33\n*Plastic\n324,0\n"
                    "*Step, name=Impact\n*Dynamic, Explicit\n,.004\n*End Step\n")
    model = reference_model()
    module.rewrite_input(str(deck), model.resolved(), {"REF": model.core_material.model_dump()})
    text = deck.read_text()
    assert "-0.77,1.45,0.47,0.0,1.6,650.0,20.0,1.0" in text
    assert "900000000.0" in text
    assert "CONTROLS=HC_DAMAGE" in text and "ELEMENT DELETION=YES" in text
    assert "*Dynamic, Explicit, ADIABATIC" in text


def test_damage_deck_restores_solver_only_global_energy_output(tmp_path):
    path = Path(__file__).parents[1] / "src/honeycomb_studio/adapters/damage_keywords.py"
    module_spec = importlib.util.spec_from_file_location("damage_keywords", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    deck = tmp_path / "energy.inp"
    deck.write_text("*Material, name=REF\n*Plastic\n324,0\n"
                    "*Step, name=Impact\n*Dynamic, Explicit\n,.004\n"
                    "*Output, history, number interval=2000\n"
                    "*Energy Output\nALLAE, ALLIE, ALLKE\n"
                    "*Energy Output, elset=CORE_ENERGY\nALLPD, ALLDMD\n*End Step\n")
    model = reference_model()
    module.rewrite_input(str(deck), model.resolved(), {"REF": model.core_material.model_dump()})
    text = deck.read_text()
    assert '*Energy Output\n' + ', '.join(model.history_variables) + '\n' in text
    assert '*Energy Output, elset=CORE_ENERGY\nALLPD, ALLDMD\n' in text

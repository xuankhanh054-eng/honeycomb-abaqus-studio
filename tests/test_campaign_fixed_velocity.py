"""A separately authorized6.5m/s run cannot reuse8m/s outcomes or mutate its inputs."""
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from honeycomb_studio.campaign import BOUNDS, campaign_manifest, record, reference_model
from honeycomb_studio.campaign_export import globally_qualified
from honeycomb_studio.campaign_protocol import advance, runnable
from honeycomb_studio.campaign_queue import admission, campaign_key, execution_source
from honeycomb_studio.models import digest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from campaign_controller import dispatch_candidates, relay


def fresh_collection():
    old = campaign_manifest()
    manifest = deepcopy(old)
    rows = []
    for row in old['designs']:
        model = reference_model(**{k: row['model'][k] for k in BOUNDS}, velocity_m_s=6.5)
        item = record(model, row['role'], int(row['id'].split('-')[-1]), row['owner'], row['varied_parameters'])
        assert item['id'] == row['id'] and item['physics_hash'] != row['physics_hash']
        rows.append(item)
    model = rows[0]['model']
    mesh_keys = ('core_centre_seed', 'core_outer_seed', 'skin_centre_seed', 'skin_outer_seed',
                 'reentrant_depth_seed', 'core_z_divisions', 'ball_seed', 'fine_radius',
                 'skin_patch_half', 'shell_element', 'shell_points')
    manifest.update(id='core-damage-v2-20261107-v6p5', designs=rows, pilot_designs=[],
                    enabled=True, phase='fixed_collection', execution_scope='full', frozen=False,
                    source_sha='a' * 40, runtime_hash='b' * 64,
                    infrastructure={r['owner']: {'passed': True} for r in rows},
                    mesh_policy={'mode': 'reuse_actions_reference', 'parameters': {k: model[k] for k in mesh_keys}},
                    provisional_collection={'enabled': True},
                    gates={k: {'passed': True, 'source_sha': 'a' * 40, 'runtime_hash': 'b' * 64}
                           for k in ('material', 'restart', 'archive_restore')},
                    fixed_collection_contract={
                        'human_authorized': True, 'velocity_m_s': 6.5, 'duration_s': .008,
                        'initial_designs_hash': digest({r['id']: r['physics_hash'] for r in rows}),
                        'previous_campaign_id': 'core-damage-v2-20261107'})
    return manifest


def test_new_campaign_namespace_is_separate_and_path_safe():
    assert campaign_key('core-damage-v2-20261107-v6p5') != campaign_key('core-damage-v2-20261107')
    for name in ('core-damage-v2-20261107/../v6p5', 'core-damage-v2-20261107-v6p5/extra'):
        with pytest.raises(ValueError):
            campaign_key(name)


def test_explicit6p5_contract_admits_exactly400_fresh_designs_and_relays():
    manifest = fresh_collection()
    result = advance(manifest)
    assert result['phase'] == 'fixed_collection'
    assert len(runnable(result)) == len(dispatch_candidates(result)) == 400
    assert all(admission(result, row) for row in result['designs'])
    assert not globally_qualified(result)
    assert all(row['status'] == 'pending' and row['segment'] == row['attempt'] == 0 for row in result['designs'])
    assert all('summary' not in row and 'predecessor' not in row and 'ticket' not in row for row in result['designs'])
    assert all(reference_model(**row['model']).resolved()['energy_J'] == pytest.approx(55.98125)
               for row in result['designs'])
    calls = []
    store = SimpleNamespace(catalog=SimpleNamespace(get=lambda _: (result, None)))
    app = SimpleNamespace(repository=lambda _: SimpleNamespace(repo=lambda method, path, body=None:
                          {'workflow_runs': []} if method == 'GET' else calls.append(body)))
    assert relay(store, app, result['id'], '11')['controller_redispatched']
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['velocity', 'duration', 'hash', 'authorization', 'material', 'source',
                                   'mesh', 'runtime', 'scope'])
def test6p5_collection_rejects_contract_and_native_proof_drift(change):
    manifest = fresh_collection()
    row = manifest['designs'][0]
    if change == 'velocity':
        row['model']['velocity_m_s'] = 8.
    elif change == 'duration':
        row['model']['duration'] = .012
    elif change == 'hash':
        row['physics_hash'] = 'c' * 64
    elif change == 'authorization':
        manifest['fixed_collection_contract']['human_authorized'] = False
    elif change == 'material':
        row['model']['core_material']['johnson_cook']['d1'] = .999
    elif change == 'source':
        manifest['gates']['restart']['source_sha'] = 'c' * 40
    elif change == 'runtime':
        manifest['gates']['restart']['runtime_hash'] = 'c' * 64
    elif change == 'scope':
        manifest['execution_scope'] = 'pilot'
    else:
        row['model']['skin_centre_seed'] *= 2
    assert not admission(manifest, row)


def test6p5_collection_still_holds_unknown_failures():
    manifest = fresh_collection()
    manifest['designs'][0].update(status='needs_investigation', error='Missing native result')
    result = advance(manifest)
    assert result['phase'] == 'needs_investigation'
    assert dispatch_candidates(result) == []


def test6p5_finishes_only_after400_verified_actual_results():
    from test_campaign_provisional_failure import complete

    manifest = fresh_collection()
    for row in manifest['designs']:
        complete(row)
        row['summary'].update(velocity_m_s=6.5, expected_energy_J=55.98125, initial_kinetic_energy_J=55.98125)
    last = manifest['designs'][-1]
    last['archive_verified'] = False
    assert advance(manifest)['phase'] == 'fixed_collection'
    last['archive_verified'] = True
    result = advance(manifest)
    assert result['phase'] == 'collection_completed' and result['enabled'] is False
    assert result['collection_report']['completed_verified'] == 400
    assert not globally_qualified(result) and 'analysis_ticket' not in result


def test_worker_metadata_upgrade_requires_native_identity_and_preserves_continuations():
    manifest = {'source_sha': 'a' * 40, 'runtime_hash': 'b' * 64,
                'qualified_worker_patch': {'passed': True, 'source_sha': 'c' * 40,
                    'base_source_sha': 'a' * 40, 'runtime_hash': 'b' * 64,
                    'native_components_unchanged_verified': True}}
    proof = {'passed': True, 'base_source_sha': 'c' * 40, 'source_sha': 'd' * 40,
             'runtime_hash': 'b' * 64, 'native_components_unchanged_verified': True,
             'unchanged_components': {str(i): 'e' * 64 for i in range(10)}}
    manifest['worker_metadata_upgrade'] = proof
    assert execution_source(manifest, {'segment': 0}) == 'd' * 40
    assert execution_source(manifest, {'segment': 1, 'predecessor': {'source_sha': 'a' * 40}}) == 'a' * 40
    for field, value in (('runtime_hash', 'f' * 64), ('base_source_sha', 'f' * 40),
                         ('native_components_unchanged_verified', False), ('unchanged_components', {})):
        manifest['worker_metadata_upgrade'] = {**proof, field: value}
        assert execution_source(manifest, {'segment': 0}) == 'c' * 40

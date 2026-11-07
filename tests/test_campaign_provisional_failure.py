"""A failed response window must remain failed during authorized raw collection."""
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from honeycomb_studio.campaign import campaign_manifest, numerical_gate
from honeycomb_studio.campaign_export import export_campaign, globally_qualified
from honeycomb_studio.campaign_protocol import advance
from honeycomb_studio.campaign_queue import admission
from honeycomb_studio.models import digest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from campaign_controller import dispatch_candidates, relay, status_report

ERROR = '8 m/s pilot did not meet the agreed response window'


def complete(row):
    row.update(status='completed', archive_verified=True, archive={'kind': 'fixture'},
               quality={'passed': True}, summary=dict(
        velocity_m_s=8., foil=row['model']['foil'], solver_completed=True,
        history_integrity_passed=True, damage_fields_complete=True,
        maximum_energy_balance_drift_percent=0., maximum_artificial_energy_ratio_developed_percent=0.,
        contact_force_check_max_relative_percent=0., expected_energy_J=84.8, initial_kinetic_energy_J=84.8,
        rear_skin_deleted=True, max_rear_downward_mm=11.46, SEA_core_J_kg=100.,
        core_mass_kg=.1, event_finished=True))


def authorized_failure():
    manifest = campaign_manifest()
    row = manifest['designs'][0]
    mesh_keys = ('core_centre_seed', 'core_outer_seed', 'skin_centre_seed', 'skin_outer_seed',
                 'reentrant_depth_seed', 'core_z_divisions', 'ball_seed', 'fine_radius',
                 'skin_patch_half', 'shell_element', 'shell_points')
    manifest.update(enabled=True, phase='pilot', execution_scope='full', source_sha='a' * 40,
                    runtime_hash='b' * 64, infrastructure={r['owner']: {'passed': True}
                                                         for r in manifest['designs']},
                    mesh_policy={'mode': 'reuse_actions_reference',
                                 'parameters': {k: row['model'][k] for k in mesh_keys}},
                    gates={k: {'passed': True, 'source_sha': 'a' * 40, 'runtime_hash': 'b' * 64}
                           for k in ('material', 'restart', 'archive_restore')})
    for pilot in manifest['pilot_designs']:
        complete(pilot)
        assert numerical_gate(pilot['summary'])['passed']
    manifest['provisional_collection'] = {'enabled': True, 'after_pilot_response_failure': {
        'enabled': True, 'failed_gate': 'pilot_response_window',
        'initial_designs_hash': digest({r['id']: r['physics_hash'] for r in manifest['designs']
                                        if r['role'] in ('development', 'test')}),
        'reason': 'Human authorized fixed-input provisional collection of the initial 400'}}
    return manifest


def test_authorized_failed_pilot_keeps_400_collection_and_relay_running(tmp_path):
    manifest = authorized_failure()
    result = advance(manifest)
    assert result['phase'] == 'provisional_collection'
    assert result['scientific_block']['error'] == ERROR
    assert result['scientific_block']['passed'] is False
    assert len(dispatch_candidates(result)) == 400
    assert all(admission(result, row) for row in dispatch_candidates(result))
    assert result['designs'] == manifest['designs']  # No physics/source/checkpoint rewrites.
    assert result['gates'] == manifest['gates']
    assert not globally_qualified(result)
    report = status_report(result)
    assert report['scientific_block']['error'] == ERROR
    assert report['protocol_error'] is None
    dispatched = []
    store = SimpleNamespace(catalog=SimpleNamespace(get=lambda _: (result, None)))
    app = SimpleNamespace(repository=lambda _: SimpleNamespace(repo=lambda method, path, body=None:
                          {'workflow_runs': []} if method == 'GET' else dispatched.append(body)))
    assert relay(store, app, manifest['id'], '11')['controller_redispatched']
    assert dispatched == [{'ref': 'main'}]
    complete(result['designs'][0])
    export_campaign(result, tmp_path)
    import csv
    with (tmp_path / 'samples.csv').open(encoding='utf-8-sig', newline='') as stream:
        assert all(r['ml_development_eligible'] == 'False' for r in csv.DictReader(stream))


def test_only_the_known_pilot_failure_can_be_resumed():
    manifest = authorized_failure()
    manifest.update(phase='needs_investigation', error=ERROR)
    assert advance(manifest)['phase'] == 'provisional_collection'
    for error in ('Unknown model error', 'Mesh convergence did not meet 5% gate'):
        manifest['error'] = error
        result = advance(manifest)
        assert result['phase'] == 'needs_investigation'
        assert dispatch_candidates(result) == []
        assert status_report(result)['protocol_error'] == error


@pytest.mark.parametrize('change', ['permission', 'plan', 'pilot', 'scope', 'runtime', 'model', 'pilot_quality'])
def test_failure_collection_cannot_bypass_its_inputs_or_native_gates(change):
    manifest = authorized_failure()
    manifest.update(phase='needs_investigation', error=ERROR)
    if change == 'permission':
        del manifest['provisional_collection']['after_pilot_response_failure']
    elif change == 'plan':
        manifest['designs'][0]['physics_hash'] = 'c' * 64
    elif change == 'pilot':
        manifest['pilot_designs'][0]['archive_verified'] = False
    elif change == 'scope':
        manifest['execution_scope'] = 'pilot'
    elif change == 'runtime':
        manifest['gates']['restart']['runtime_hash'] = 'c' * 64
    elif change == 'pilot_quality':
        manifest['pilot_designs'][0]['quality']['passed'] = False
    else:
        manifest['designs'][0]['model']['velocity_m_s'] = 9.
    result = advance(manifest)
    assert not admission(result, result['designs'][0])


def test_collection_finishes_only_after_400_actual_archives_without_scientific_acceptance():
    manifest = advance(authorized_failure())
    for row in manifest['designs']:
        complete(row)
    row = manifest['designs'][0]
    row['archive_verified'] = False
    assert advance(manifest)['phase'] == 'provisional_collection'
    row['archive_verified'] = True
    result = advance(manifest)
    assert result['phase'] == 'collection_completed'
    assert result['enabled'] is False
    assert result['collection_report']['completed_verified'] == 400
    assert not globally_qualified(result)
    assert 'analysis_ticket' not in result
    assert dispatch_candidates(result) == []


def test_unexpected_execution_block_still_holds_authorized_collection():
    manifest = advance(authorized_failure())
    manifest['designs'][0].update(status='needs_investigation', error='Retained artifact is missing')
    result = advance(manifest)
    assert result['phase'] == 'needs_investigation'
    assert result['blocked_design_ids'] == [manifest['designs'][0]['id']]
    assert dispatch_candidates(result) == []
    assert advance(deepcopy(result))['phase'] == 'needs_investigation'


def test_existing_execution_block_is_held_on_the_pilot_transition():
    manifest = authorized_failure()
    manifest['designs'][0].update(status='quarantined', error='Numerical quality exhausted')
    result = advance(manifest)
    assert result['phase'] == 'needs_investigation'
    assert result['blocked_design_ids'] == [manifest['designs'][0]['id']]


def test_frozen_flags_cannot_bypass_native_proof_during_provisional_collection():
    manifest = advance(authorized_failure())
    manifest['frozen'] = True
    manifest['gates'].update({k: {'passed': True} for k in
                             ('time', 'mesh', 'damage_sensitivity', 'development_time_audit')})
    assert not globally_qualified(manifest)
    manifest['gates']['restart']['runtime_hash'] = 'c' * 64
    assert not admission(manifest, manifest['designs'][0])

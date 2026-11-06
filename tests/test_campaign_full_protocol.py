from collections import Counter

import numpy as np
import pytest

from honeycomb_studio import campaign_ml
from honeycomb_studio.campaign import campaign_manifest, freeze
from honeycomb_studio.campaign_protocol import advance, qualification_designs


def accepted(row, displacement=2.):
    row.update(status='completed', archive_verified=True, quality={'passed': True}, summary=dict(
        velocity_m_s=row['model']['velocity_m_s'], foil=row['model']['foil'],
        solver_completed=True, history_integrity_passed=True, damage_fields_complete=True,
        maximum_energy_balance_drift_percent=0., maximum_artificial_energy_ratio_developed_percent=0.,
        contact_force_check_max_relative_percent=0., expected_energy_J=84.8, initial_kinetic_energy_J=84.8,
        rear_skin_deleted=False, max_rear_downward_mm=displacement, SEA_core_J_kg=100.,
        core_mass_kg=.1, event_finished=True))
    return row


def test_full_campaign_does_not_change_velocity_for_a_small_pilot_response():
    manifest = campaign_manifest()
    manifest.update(phase='pilot', execution_scope='full')
    for row in manifest['pilot_designs']:
        accepted(row, .2)
    result = advance(manifest)
    assert result['phase'] == 'needs_investigation'
    assert len(result['pilot_designs']) == 3


def test_controller_overlaps_only_explicitly_prefetched_qualification_checks():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
    from campaign_controller import dispatch_candidates

    manifest = campaign_manifest()
    manifest.update(phase='pilot', execution_scope='full')
    checks = qualification_designs(8., .012)
    manifest['designs'].extend(checks)
    manifest['prefetched_qualification_ids'] = [checks[0]['id'], manifest['designs'][0]['id']]
    candidates = dispatch_candidates(manifest)
    assert {r['id'] for r in candidates} == {r['id'] for r in manifest['pilot_designs']} | {checks[0]['id']}
    manifest['provisional_collection'] = {'enabled': True}
    assert len(dispatch_candidates(manifest)) == 404
    manifest['phase'] = 'needs_investigation'
    assert dispatch_candidates(manifest) == []
    manifest['phase'] = 'pilot'
    manifest['execution_scope'] = 'pilot'
    assert dispatch_candidates(manifest) == manifest['pilot_designs']


def test_controller_relay_stops_when_blocked_disabled_or_already_queued():
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
    from campaign_controller import relay

    metadata = {'enabled': True, 'phase': 'pilot'}
    runs = [{'id': 11, 'status': 'in_progress'}]
    dispatched = []
    store = SimpleNamespace(catalog=SimpleNamespace(get=lambda _: (metadata, None)))

    def request(method, path, body=None):
        if method == 'GET':
            return {'workflow_runs': runs}
        dispatched.append(body)

    app = SimpleNamespace(repository=lambda _: SimpleNamespace(repo=request))
    assert relay(store, app, 'core-damage-v2-20261107', '11')['controller_redispatched']
    assert dispatched == [{'ref': 'main'}]
    runs.append({'id': 12, 'status': 'queued'})
    assert not relay(store, app, 'core-damage-v2-20261107', '11')['controller_redispatched']
    runs.pop()
    metadata['enabled'] = False
    assert not relay(store, app, 'core-damage-v2-20261107', '11')['controller_redispatched']
    metadata['enabled'] = True
    for phase in ('needs_investigation', 'completed', 'verification_failed', 'insufficient_data'):
        metadata['phase'] = phase
        assert not relay(store, app, 'core-damage-v2-20261107', '11')['controller_redispatched']
    assert len(dispatched) == 1


def test_controller_refills_a_released_slot_and_counts_its_own_actions_job(monkeypatch):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
    import campaign_controller as controller

    manifest = campaign_manifest()
    owner = manifest['designs'][0]['owner']
    manifest.update(enabled=True, phase='production', capacity_stage=160,
                    workflow_capacity=160, license_capacity=128)
    jobs = [{'status': 'in_progress', 'campaign_solver': True} for _ in range(19)]
    jobs.append({'status': 'in_progress', 'campaign_solver': False})
    dispatched = []

    def reserve(_manifest, row):
        dispatched.append(row['id'])
        row['status'] = 'dispatching'

    monkeypatch.setattr(controller, 'page_jobs', lambda _github, **_kwargs: list(jobs))
    monkeypatch.setattr(controller, 'admission', lambda _manifest, _row: True)
    monkeypatch.setattr(controller, 'Dispatcher', lambda *_args: SimpleNamespace(reserve=reserve))
    app = SimpleNamespace(repository=lambda _: object())
    controller.fill_owner_slots(None, app, manifest, manifest['designs'], 160, owner)
    assert not dispatched
    jobs.pop(0)  # A real worker finishes while the controller still occupies one Actions slot.
    controller.fill_owner_slots(None, app, manifest, manifest['designs'], 160, owner)
    assert len(dispatched) == 1
    controller.fill_owner_slots(None, app, manifest, manifest['designs'], 160, owner)
    assert len(dispatched) == 1  # The durable reservation consumes the released slot.
    report = controller.status_report(manifest)
    assert report['workflow_capacity'] == 160 and report['solver_license_capacity'] == 128


def test_geometry_patch_requires_completed_native_evidence_and_matching_inputs():
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
    from campaign_controller import ingest_geometry_patch

    manifest = campaign_manifest()
    rows = manifest['designs'][:8]
    expected = 'c' * 40
    manifest.update(source_sha='a' * 40, runtime_hash='b' * 64, pending_worker_patch_source=expected,
                    bond_repair_design_ids=[r['id'] for r in rows])
    rows[0].update(status='model_patch_wait', repair_waiting_for_source=expected, attempt=2)
    report = {'passed': True, 'source_sha': expected, 'base_source_sha': 'a' * 40, 'runtime_hash': 'b' * 64,
              'workflow_run_id': 11, 'unchanged_native_components': {str(i): 'd' * 64 for i in range(10)},
              'cases': [{'id': r['id'], 'passed': True, 'solver_completed': True, 'mass_preserved': True,
                         'physical_inputs_hash': r['physical_inputs_hash']} for r in rows]}
    workflow = {'status': 'in_progress', 'conclusion': None, 'head_sha': expected,
                'path': '.github/workflows/core-damage-bond-probe.yml'}
    store = SimpleNamespace(catalog=SimpleNamespace(get=lambda _: (report, None),
                            github=SimpleNamespace(repo=lambda *_: workflow)))
    assert 'qualified_worker_patch' not in ingest_geometry_patch(store, manifest)
    workflow.update(status='completed', conclusion='success')
    original_hash = report['cases'][0]['physical_inputs_hash']
    report['cases'][0]['physical_inputs_hash'] = 'e' * 64
    assert 'qualified_worker_patch' not in ingest_geometry_patch(store, manifest)
    report['cases'][0]['physical_inputs_hash'] = original_hash
    result = ingest_geometry_patch(store, manifest)
    assert result['qualified_worker_patch']['native_components_unchanged_verified']
    assert rows[0]['status'] == 'preparation_retry' and rows[0]['attempt'] == 0
    assert result['source_sha'] == 'a' * 40


def test_failed_eight_ms_time_gate_does_not_allocate_sixteen_ms():
    manifest = campaign_manifest()
    manifest.update(phase='time', execution_scope='full', selected_velocity_m_s=8., trial_duration_s=.008)
    for row in manifest['pilot_designs']:
        accepted(row)
    manifest['designs'].extend(accepted(row, 4.) for row in qualification_designs(8., .012))
    result = advance(manifest)
    assert result['phase'] == 'needs_investigation'
    assert not any(row['model']['duration'] > .012 for row in result['designs'])


def test_user_selected_actions_mesh_skips_refinement_without_claiming_convergence():
    manifest = campaign_manifest()
    manifest.update(phase='time', execution_scope='full', selected_velocity_m_s=8., trial_duration_s=.008,
                    mesh_policy={'mode': 'reuse_actions_reference', 'source_job_id': 'previous-actions'})
    for row in manifest['pilot_designs']:
        accepted(row)
    manifest['designs'].extend(accepted(row) for row in qualification_designs(8., .012))
    result = advance(manifest)
    assert result['phase'] == 'sensitivity' and result['selected_mesh_factor'] == 1.
    assert result['gates']['mesh']['convergence_verified'] is False
    assert not any(row.get('purpose') in ('mesh', 'mesh_time') for row in result['designs'])
    assert len({row['owner'] for row in result['designs'] if row.get('purpose') == 'sensitivity'}) == 8


def test_identical_qualified_control_reuses_verified_bytes_and_preserves_identity():
    from honeycomb_studio.campaign_protocol import extend_unique

    manifest = campaign_manifest()
    source = accepted(manifest['pilot_designs'][0])
    source.update(archive={'kind': 'fixture'}, source_sha='a' * 40, workflow_run_id=11)
    target = qualification_designs(8., .008, label='sensitivity')[0]
    extend_unique(manifest, [target])
    row = manifest['designs'][-1]
    assert row['id'] != source['id'] and row['physics_hash'] == source['physics_hash']
    assert row['status'] == 'completed' and row['reused_from']['design_id'] == source['id']
    assert row['source_sha'] == source['source_sha'] and row['archive'] == source['archive']
    source['summary']['SEA_core_J_kg'] = -1
    assert row['summary']['SEA_core_J_kg'] == 100.


@pytest.mark.parametrize(('velocity', 'duration'), [(5., .008), (8., .012), (8., .004)])
def test_freeze_rejects_changes_to_the_fixed_load_and_main_duration(velocity, duration):
    manifest = campaign_manifest()
    manifest['source_sha'] = 'a' * 40
    gates = {name: {'passed': True} for name in ('material', 'mesh', 'time', 'damage_sensitivity', 'archive_restore')}
    with pytest.raises(ValueError, match='fixed|8 m/s|8 ms'):
        freeze(manifest, velocity, duration, 1., gates)


def test_freeze_cannot_rewrite_dispatched_provisional_inputs():
    manifest = campaign_manifest()
    manifest['source_sha'] = 'a' * 40
    row = manifest['designs'][0]
    row['status'] = 'dispatching'
    original = row['physics_hash']
    gates = {name: {'passed': True} for name in ('material', 'mesh', 'time', 'damage_sensitivity', 'archive_restore')}
    with pytest.raises(ValueError, match='already dispatched'):
        freeze(manifest, 8., .008, .75, gates)
    assert row['physics_hash'] == original
    assert freeze(manifest, 8., .008, 1., gates)['designs'][0]['physics_hash'] == original


def test_refined_mesh_gets_its_own_time_comparison_before_sensitivity():
    manifest = campaign_manifest()
    manifest.update(phase='mesh', execution_scope='full', selected_velocity_m_s=8., selected_duration_s=.008)
    for factor, displacement in ((1., 3.), (.75, 2.), (.5, 2.01)):
        manifest['designs'].extend(accepted(row, displacement) for row in qualification_designs(
            8., .008, mesh_factor=factor, label='mesh'))
    result = advance(manifest)
    assert result['phase'] == 'mesh_time'
    checks = [row for row in result['designs'] if row.get('purpose') == 'mesh_time']
    assert len(checks) == 3
    assert all(row['mesh_factor'] == .75 and row['model']['duration'] == .012 for row in checks)
    for row in checks:
        accepted(row, 2.)
    assert advance(result)['phase'] == 'sensitivity'


def test_adaptive_rounds_have_twenty_unique_designs_and_stop_at_eighty(monkeypatch):
    manifest = campaign_manifest()
    accepted(manifest['designs'][0])
    monkeypatch.setattr(campaign_ml, 'core_mass', lambda point: .05)
    original_pool = campaign_ml.candidate_pool
    monkeypatch.setattr(campaign_ml, 'candidate_pool', lambda m: original_pool(m, count=100))

    class Predictor:
        def __init__(self, offset):
            self.offset = offset

        def predict(self, x):
            return np.column_stack((x[:, 0] + self.offset, x[:, 1] * (1 + self.offset)))

    fitted = {'one': Predictor(0.), 'two': Predictor(1.)}
    ids = set()
    for _ in range(4):
        rows = campaign_ml.adaptive_designs(manifest, fitted)
        assert len(rows) == 20
        assert Counter(row['selection_reason'] for row in rows) == {
            'uncertainty': 10, 'near_pareto': 6, 'sparse_domain': 4}
        assert not ids.intersection(row['id'] for row in rows)
        ids.update(row['id'] for row in rows)
        manifest['designs'].extend(accepted(row) for row in rows)
    assert len(ids) == 80
    with pytest.raises(ValueError, match='budget'):
        campaign_ml.adaptive_designs(manifest, fitted)


def test_surrogate_mass_uses_h20_and_double_walls(monkeypatch):
    calls = []

    def walls(width, height, *args):
        calls.append((width, height))
        return [((0., 0.), (3., 4.), False), ((0., 0.), (3., 0.), True)]

    campaign_ml.core_mass.cache_clear()
    monkeypatch.setattr(campaign_ml, 'network', walls)
    assert campaign_ml.core_mass((35., .0635, 6.35, 5.)) == pytest.approx(11 * 100 * .0635 * 2700e-9)
    assert calls == [(100., 20.)]
    campaign_ml.core_mass.cache_clear()


def test_fe_confirmation_covers_extremes_balance_and_new_tradeoffs():
    manifest = campaign_manifest()
    points = campaign_ml.candidate_pool(manifest, count=40)
    # A known point and a duplicate must not consume either of the twenty FE slots.
    candidates = np.vstack((points, points[0], [manifest['designs'][0]['model'][k] for k in manifest['bounds']]))
    x = np.linspace(0., 1., len(candidates))
    objectives = np.column_stack((-x, x))
    rows = campaign_ml.confirmation_selection(manifest, candidates, objectives)
    assert len(rows) == len({row['physics_hash'] for row in rows}) == 20
    assert Counter(row['selection_reason'] for row in rows) == {
        'pareto_extreme_sea': 1, 'pareto_extreme_rear': 1, 'pareto_balance': 6, 'pareto_coverage': 12}
    assert rows[0]['model']['reentrant_delta_deg'] == points[-1, 0]
    assert rows[1]['model']['reentrant_delta_deg'] == points[0, 0]

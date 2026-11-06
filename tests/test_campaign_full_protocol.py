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
    manifest['execution_scope'] = 'pilot'
    assert dispatch_candidates(manifest) == manifest['pilot_designs']


def test_failed_eight_ms_time_gate_does_not_allocate_sixteen_ms():
    manifest = campaign_manifest()
    manifest.update(phase='time', execution_scope='full', selected_velocity_m_s=8., trial_duration_s=.008)
    for row in manifest['pilot_designs']:
        accepted(row)
    manifest['designs'].extend(accepted(row, 4.) for row in qualification_designs(8., .012))
    result = advance(manifest)
    assert result['phase'] == 'needs_investigation'
    assert not any(row['model']['duration'] > .012 for row in result['designs'])


@pytest.mark.parametrize(('velocity', 'duration'), [(5., .008), (8., .012), (8., .004)])
def test_freeze_rejects_changes_to_the_fixed_load_and_main_duration(velocity, duration):
    manifest = campaign_manifest()
    manifest['source_sha'] = 'a' * 40
    gates = {name: {'passed': True} for name in ('material', 'mesh', 'time', 'damage_sensitivity', 'archive_restore')}
    with pytest.raises(ValueError, match='fixed|8 m/s|8 ms'):
        freeze(manifest, velocity, duration, 1., gates)


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

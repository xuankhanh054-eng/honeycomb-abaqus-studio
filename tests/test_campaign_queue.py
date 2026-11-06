import copy

import pytest

from honeycomb_studio.campaign import campaign_manifest
from honeycomb_studio.campaign_queue import (
    admission,
    apply_outcome,
    available_slots,
    claim,
    dispatch_capacity,
    page_jobs,
    ramp_capacity,
)


def test_claim_cannot_start_duplicate_or_mismatched_continuation():
    design = campaign_manifest()['pilot_designs'][0]
    work = dict(ticket='a' * 32, repository=design['repository'], source_sha='b' * 40,
                design=design, segment=0, status='ready')
    claimed = claim(work, ticket='a' * 32, workflow=11, source_sha='b' * 40, repository=design['repository'])
    for workflow in (11, 12):
        with pytest.raises(ValueError, match='duplicate'):
            claim(claimed, ticket='a' * 32, workflow=workflow, source_sha='b' * 40, repository=design['repository'])
    with pytest.raises(ValueError, match='predecessor'):
        claim({**work, 'segment': 1}, ticket='a' * 32, workflow=12, source_sha='b' * 40,
              repository=design['repository'])


def test_production_cannot_bypass_material_and_forty_design_time_audit():
    manifest = campaign_manifest()
    row = manifest['designs'][0]
    manifest.update(enabled=True, execution_scope='full', source_sha='a' * 40, runtime_hash='b' * 64, frozen=True,
                    infrastructure={row['owner']: {'passed': True}}, gates={k: {'passed': True} for k in
                    ('material', 'time', 'mesh', 'damage_sensitivity', 'archive_restore')})
    assert not admission(manifest, row)

    manifest['audit_ids'] = [row['id']]
    assert admission(manifest, row)
    assert not admission({**manifest, 'execution_scope': 'pilot'}, row)
    manifest['gates']['material']['passed'] = False
    assert not admission(manifest, row)


def test_provisional_collection_requires_exact_native_proof_and_stops_when_blocked(tmp_path):
    import csv

    from honeycomb_studio.campaign_export import export_campaign

    manifest = campaign_manifest()
    row = manifest['designs'][0]
    mesh_keys = ('core_centre_seed', 'core_outer_seed', 'skin_centre_seed', 'skin_outer_seed',
                 'reentrant_depth_seed', 'core_z_divisions', 'ball_seed', 'fine_radius',
                 'skin_patch_half', 'shell_element', 'shell_points')
    native = {k: {'passed': True, 'source_sha': 'a' * 40, 'runtime_hash': 'b' * 64}
              for k in ('material', 'restart', 'archive_restore')}
    manifest.update(enabled=True, execution_scope='full', phase='pilot', source_sha='a' * 40,
                    runtime_hash='b' * 64, infrastructure={row['owner']: {'passed': True}}, gates=native,
                    mesh_policy={'mode': 'reuse_actions_reference', 'parameters': {k: row['model'][k] for k in mesh_keys}})
    assert not admission(manifest, row)
    manifest['provisional_collection'] = {'enabled': True}
    assert admission(manifest, row)
    assert not admission(manifest, {**row, 'model': {**row['model'], 'velocity_m_s': 9}})
    manifest['gates']['restart']['source_sha'] = 'c' * 40
    assert not admission(manifest, row)
    manifest['gates']['restart']['source_sha'] = manifest['source_sha']
    manifest['phase'] = 'needs_investigation'
    assert not admission(manifest, row)
    manifest['phase'] = 'pilot'
    row.update(status='completed', archive_verified=True, quality={'passed': True},
               summary={'rear_skin_deleted': False, 'event_finished': True})
    manifest.update(designs=[row], pilot_designs=[])
    export_campaign(manifest, tmp_path)
    with (tmp_path / 'samples.csv').open(encoding='utf-8-sig', newline='') as stream:
        exported = next(csv.DictReader(stream))
    assert exported['ml_development_eligible'] == 'False'
    manifest.update(frozen=True)
    manifest['gates'].update({k: {'passed': True} for k in ('time', 'mesh', 'damage_sensitivity', 'development_time_audit')})
    export_campaign(manifest, tmp_path)
    with (tmp_path / 'samples.csv').open(encoding='utf-8-sig', newline='') as stream:
        assert next(csv.DictReader(stream))['ml_development_eligible'] == 'True'


def test_checkpoint_and_retries_are_bounded_and_upload_failure_never_restarts_solver():
    row = {**campaign_manifest()['pilot_designs'][0], 'ticket': 'a' * 32}
    output = dict(ticket=row['ticket'], design=row, outcome='checkpoint', segment=3, workflow_run_id=11,
                  archive_verified=True, receipt={}, archive={})
    assert apply_outcome(row, output)['status'] == 'budget_exhausted'
    output['archive_verified'] = False
    with pytest.raises(ValueError, match='Unverified'):
        apply_outcome(row, output)
    failed = {**output, 'outcome': 'solver_failed'}
    assert apply_outcome({**row, 'attempt': 2}, failed)['status'] == 'quarantined'
    assert apply_outcome(row, {**failed, 'outcome': 'upload_failed'})['status'] == 'publication_pending'
    assert row['status'] == 'pending'


def test_quota_counts_all_active_jobs_and_reserved_dispatches():
    jobs = [{'status': 'in_progress'}] * 11 + [{'status': 'queued'}] * 3 + [{'status': 'completed'}] * 9
    assert available_slots(20, jobs, reserved=2) == 4
    assert available_slots(8, jobs) == 0
    assert available_slots(200, [], reserved=1) == 19


def test_workflow_capacity_can_fill_160_actions_slots_with_a_smaller_license_pool():
    manifest = {'capacity_stage': 160, 'workflow_capacity': 160, 'license_capacity': 128}
    assert dispatch_capacity(manifest) == 160
    assert manifest['license_capacity'] == 128
    controller = {'status': 'in_progress', 'campaign_solver': False}
    assert sum(available_slots(20, [controller] if owner == 0 else []) for owner in range(8)) == 159
    assert dispatch_capacity({'capacity_stage': 160, 'license_capacity': 128}) == 128
    assert dispatch_capacity({**manifest, 'workflow_capacity': 200}) == 160
    assert dispatch_capacity({**manifest, 'capacity_stage': 0}) == 0


def test_controller_does_not_consume_the_solver_budget_but_still_uses_owner_quota():
    controller = {'status': 'in_progress', 'campaign_solver': False}
    worker = {'status': 'in_progress', 'campaign_solver': True}
    assert available_slots(1, [controller]) == 1
    assert available_slots(1, [controller, worker]) == 0
    assert available_slots(20, [controller] * 19) == 1
    assert available_slots(20, [controller] * 20) == 0
    assert available_slots(1, [controller], reserved=1) == 0


def test_actions_inventory_separates_controller_from_worker_and_unknown_workflows():
    class Actions:
        repository = 'xuankhanh24/honeycomb-abaqus-studio'
        runs = [{'id': 1, 'path': '.github/workflows/core-damage-controller.yml'}]

        def request(self, method, path, **kwargs):
            if path.startswith('/users/'):
                return [{'full_name': self.repository}]
            if '/jobs?' in path:
                return {'total_count': 1, 'jobs': [{'status': 'in_progress'}]}
            return {'total_count': len(self.runs), 'workflow_runs': self.runs} if 'status=in_progress' in path else {}

    github = Actions()
    assert available_slots(1, page_jobs(github)) == 1
    github.runs.append({'id': 2, 'path': '.github/workflows/core-damage-worker.yml'})
    assert available_slots(1, page_jobs(github)) == 0
    github.runs[-1] = {'id': 2}
    assert available_slots(1, page_jobs(github)) == 0


def test_checkpoint_resume_keeps_model_identity():
    row = {**campaign_manifest()['pilot_designs'][0], 'ticket': 'a' * 32}
    before = copy.deepcopy(row['model'])
    output = dict(ticket=row['ticket'], design=row, outcome='checkpoint', segment=0,
                  archive_verified=True, receipt={'physics_hash': row['physics_hash']}, archive={})
    after = apply_outcome(row, output)
    assert after['segment'] == 1 and after['model'] == before and after['status'] == 'checkpoint'


def test_result_keeps_actual_source_and_continuation_dispatch_uses_predecessor(monkeypatch):
    from types import SimpleNamespace

    from honeycomb_studio.campaign_queue import Dispatcher
    row = {**campaign_manifest()['pilot_designs'][0], 'ticket': 'a' * 32}
    source = 'b' * 40
    result = dict(ticket=row['ticket'], design=row, source_sha=source, outcome='checkpoint', segment=0,
                  archive_verified=True, receipt={'physics_hash': row['physics_hash'], 'source_sha': source}, archive={})
    after = apply_outcome(row, result)
    assert after['source_sha'] == after['execution_history'][-1]['source_sha'] == source
    calls, tickets = [], []
    github = SimpleNamespace(repo=lambda *args: calls.append(args))
    store = SimpleNamespace(update_design=lambda _id, _owner, _row, transform: {'designs': [transform(after)]})
    monkeypatch.setattr('honeycomb_studio.campaign_queue.admission', lambda *args: True)
    monkeypatch.setattr('honeycomb_studio.campaign_queue.Catalog', lambda _: SimpleNamespace(
        update=lambda _key, transform: tickets.append(transform(None))))
    Dispatcher(store, SimpleNamespace(repository=lambda _: github)).reserve({'id': 'core-damage-v2-20261107', 'source_sha': 'c' * 40,
                                                                           'runtime_hash': 'd' * 64}, after)
    assert tickets[0]['source_sha'] == source
    assert calls[0][2]['inputs']['source_sha'] == source


def test_capacity_ramp_accepts_a_license_ceiling_between_planned_stages():
    manifest = {'capacity_stage': 80, 'license_capacity': 128, 'pilot_designs': [],
                'designs': [dict(archive_verified=True, quality={'passed': True},
                                 solver_started_at=1, solver_finished_at=100) for _ in range(128)]}
    assert ramp_capacity(manifest)['capacity_stage'] == 128
    assert ramp_capacity(manifest)['capacity_stage'] == 128

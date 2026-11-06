"""GitHub-hosted campaign tick: reconcile durable receipts, advance gates, then fill free slots."""
import argparse
import json
import math
import os
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from honeycomb_studio.campaign import OWNERS, campaign_manifest, progress
from honeycomb_studio.campaign_acceptance import import_evidence
from honeycomb_studio.campaign_export import globally_qualified
from honeycomb_studio.campaign_protocol import advance, runnable
from honeycomb_studio.campaign_queue import (
    CampaignStore,
    Dispatcher,
    admission,
    apply_outcome,
    available_slots,
    campaign_key,
    dispatch_capacity,
    page_jobs,
    ramp_capacity,
    ticket_key,
)
from honeycomb_studio.cloud_github import CampaignApp, Catalog

CANONICAL = 'xuankhanh24/honeycomb-abaqus-studio'


def ingest_evidence(store, manifest, app):
    evidence, _ = store.catalog.get('campaign-validation/latest')
    if evidence and evidence.get('source_sha') == manifest['source_sha']:
        workflow = store.catalog.github.repo('GET', '/actions/runs/' + str(evidence['workflow_run_id']))
        if workflow.get('status') == 'completed':
            manifest = import_evidence(manifest, evidence, workflow)
    for owner in OWNERS:
        github = app.repository(owner + '/honeycomb-abaqus-studio')
        report, _ = Catalog(github).get('campaign-infrastructure/latest')
        if not report or any(report.get(k) != manifest.get(k) for k in ('source_sha', 'runtime_hash')):
            continue
        workflow = github.repo('GET', '/actions/runs/' + str(report['workflow_run_id']))
        if (workflow.get('status') == 'completed' and workflow.get('conclusion') == 'success'
                and workflow.get('path', '').split('@')[0] in (
                    '.github/workflows/core-damage-validate.yml', '.github/workflows/core-damage-infrastructure.yml')):
            manifest.setdefault('infrastructure', {})[owner] = report
    return ingest_geometry_patch(store, manifest)


def ingest_geometry_patch(store, manifest):
    expected = manifest.get('pending_worker_patch_source')
    if not expected:
        return manifest
    report, _ = store.catalog.get('campaign-geometry-patch/latest')
    if (not report or report.get('passed') is not True or report.get('source_sha') != expected
            or report.get('base_source_sha') != manifest['source_sha']
            or report.get('runtime_hash') != manifest['runtime_hash']):
        return manifest
    workflow = store.catalog.github.repo('GET', '/actions/runs/' + str(report['workflow_run_id']))
    if (workflow.get('status') != 'completed' or workflow.get('conclusion') != 'success'
            or workflow.get('head_sha') != expected
            or workflow.get('path', '').split('@')[0] != '.github/workflows/core-damage-bond-probe.yml'):
        return manifest
    rows = {r['id']: r for r in manifest['designs']}
    cases = report.get('cases', [])
    expected_ids = set(manifest.get('bond_repair_design_ids', []))
    if (len(cases) != 8 or {c['id'] for c in cases} != expected_ids
            or not all(c.get('passed') is True and c.get('solver_completed') is True
                       and c.get('mass_preserved') is True
                       and c.get('physical_inputs_hash') == rows[c['id']]['physical_inputs_hash'] for c in cases)
            or len(report.get('unchanged_native_components', {})) != 10):
        return manifest
    manifest['qualified_worker_patch'] = {**report, 'native_components_unchanged_verified': True}
    for row in manifest['designs']:
        if row.get('repair_waiting_for_source') == expected and row['status'] == 'model_patch_wait':
            row.update(status='preparation_retry', attempt=0, segment=0, predecessor=None, error=None)
    return manifest


def persist(store, manifest):
    key = campaign_key(manifest['id'])
    for owner in OWNERS:
        rows = [r for r in manifest['designs'] + manifest['pilot_designs'] if r['owner'] == owner]
        store.catalog.update(key + '/owners/' + owner, lambda old, rows=rows: {'designs': rows})
    metadata = {k: v for k, v in manifest.items() if k not in ('designs', 'pilot_designs')}
    store.catalog.update(key + '/meta', lambda old: metadata)


def reconcile(store, manifest, app):
    with ThreadPoolExecutor(max_workers=len(OWNERS)) as pool:
        list(pool.map(lambda owner: reconcile_owner(store, manifest, app, owner), OWNERS))
    return manifest


def reconcile_owner(store, manifest, app, owner):
    for row in manifest['designs'] + manifest['pilot_designs']:
        if row['owner'] != owner:
            continue
        if row['status'] == 'publication_pending':
            reconcile_publication(manifest, row, app)
            continue
        if row['status'] != 'dispatching' or not row.get('ticket'):
            continue
        github = app.repository(row['repository'])
        work, _ = Catalog(github).get(ticket_key(manifest['id'], row['ticket']))
        if work is None:
            # Never assume a lost request means a solver did not start.
            row['dispatch_error'] = 'Missing execution ticket; needs reconciliation'
            if time.time() - row['dispatch_started_at'] > 900:
                row.update(status='needs_investigation', error='Reserved execution ticket is missing; inspect central and repository catalogs')
            continue
        if work.get('status') == 'finished':
            updated = apply_outcome(row, work)
            row.clear()
            row.update(updated)
        elif work.get('workflow_run_id'):
            row['workflow_run_id'] = work['workflow_run_id']
            row['phase'] = work.get('phase', 'preparing')
            if work.get('status') == 'claimed' and not work.get('error'):
                row['error'] = None
            run = github.repo('GET', '/actions/runs/' + str(work['workflow_run_id']))
            if run['status'] == 'completed':
                row.update(status='needs_investigation', error='Workflow ended without a verified solver/publication receipt',
                           workflow_conclusion=run['conclusion'])
        elif time.time() - row['dispatch_started_at'] > 900:
            runs = github.repo('GET', '/actions/workflows/core-damage-worker.yml/runs?event=workflow_dispatch&per_page=100')
            match = [r for r in runs['workflow_runs'] if r.get('display_title') == 'core-damage · ' + row['ticket']]
            if match:
                row['workflow_run_id'] = match[0]['id']
            else:
                # Re-dispatch the SAME ticket. The CAS claim rejects duplicate workers even after uncertain responses.
                github.repo('POST', '/actions/workflows/core-damage-worker.yml/dispatches',
                    {'ref': 'main', 'inputs': {'campaign_id': manifest['id'], 'ticket': row['ticket'],
                                               'source_sha': work['source_sha']}})
                row['dispatch_started_at'] = time.time()
    return manifest


def reconcile_publication(manifest, row, app):
    github = app.repository(row['repository'])
    catalog = Catalog(github)
    key = ticket_key(manifest['id'], row['publication_ticket'])
    work, _ = catalog.get(key)
    if work and work.get('outcome') in ('completed', 'checkpoint') and work.get('archive_verified'):
        updated = apply_outcome({**row, 'ticket': row['publication_ticket']}, work)
        row.clear()
        row.update(updated)
        return
    if not work:
        row.update(status='needs_investigation', error='Publication ticket is missing')
        return
    original = github.repo('GET', '/actions/runs/' + str(work['workflow_run_id']))
    if original['status'] != 'completed':
        return  # The original runner is still retaining its computed bytes.
    publication_run = work.get('publication_workflow_run_id')
    if publication_run:
        run = github.repo('GET', '/actions/runs/' + str(publication_run))
        if run['status'] != 'completed':
            return
        if work.get('publication_attempt', 0) >= 3:
            row.update(status='needs_investigation', error='Three upload-only attempts failed; solver output remains retained')
            return
    elif work.get('publication_status') == 'ready':
        if time.time() - work['publication_dispatched_at'] < 900:
            return
        runs = github.repo('GET', '/actions/workflows/core-damage-publication.yml/runs?event=workflow_dispatch&per_page=100')
        if any(r.get('display_title') == 'core-damage-upload · ' + work['ticket'] and r['status'] != 'completed'
               for r in runs['workflow_runs']):
            return
    artifacts = github.repo('GET', f"/actions/runs/{work['workflow_run_id']}/artifacts?per_page=100")
    retained = [a for a in artifacts['artifacts'] if a['name'] == row['recoverable_artifact'] and not a['expired']]
    if len(retained) != 1:
        row.update(status='needs_investigation', error='Computed output artifact unavailable; do not restart the solver for an upload error')
        return
    catalog.update(key, lambda old: {**old, 'publication_status': 'ready', 'publication_workflow_run_id': None,
                   'publication_attempt': old.get('publication_attempt', 0) + 1,
                   'publication_dispatched_at': time.time(), 'fallback_artifact_id': retained[0]['id']})
    github.repo('POST', '/actions/workflows/core-damage-publication.yml/dispatches', {
        'ref': 'main', 'inputs': {'campaign_id': manifest['id'], 'ticket': work['ticket'],
        'source_sha': work['source_sha'], 'original_run_id': str(work['workflow_run_id'])}})


def status_report(manifest):
    report = progress(manifest)
    report.update(phase=manifest['phase'], enabled=manifest['enabled'], errors=[], owners={})
    initial = [r for r in manifest['designs'] if r['role'] in ('development', 'test')]
    report.update(initial_400_dispatched=sum(r['status'] != 'pending' for r in initial),
                  initial_400_completed=sum(r['status'] == 'completed' and r.get('archive_verified', False)
                                            and r.get('quality', {}).get('passed', False) for r in initial),
                  global_qualification_passed=globally_qualified(manifest),
                  provisional_collection=manifest.get('provisional_collection', {}).get('enabled', False))
    samples = [r for r in manifest['designs'] if r['role'] in ('development', 'adaptive', 'test', 'confirmation')]
    done = [r for r in samples if r['status'] == 'completed' and r.get('archive_verified')]
    for owner in OWNERS:
        rows = [r for r in samples if r['owner'] == owner]
        report['owners'][owner] = dict(Counter(r['status'] for r in rows))
    report.update(training_configurations=len(samples), archived_configurations=len(done),
                  percent=100 * len(done) / max(1, len(samples)))
    measured = [r.get('execution_wall_seconds') for r in done if r.get('execution_wall_seconds')]
    capacity = dispatch_capacity(manifest)
    report.update(workflow_capacity=capacity, solver_license_capacity=manifest.get('license_capacity', 0))
    report['eta_seconds'] = (math.ceil((len(samples) - len(done)) / capacity) * sum(measured) / len(measured)
                             if measured and capacity else None)
    report['errors'] = [{'id': r['id'], 'owner': r['owner'], 'status': r['status'], 'error': r.get('error')}
                        for r in manifest['designs'] + manifest['pilot_designs'] if r.get('error')]
    report['gates'] = manifest.get('gates', {})
    report['final_report_url'] = manifest.get('final_report_url')
    report['quota_observation'] = manifest.get('quota_observation', {})
    report['observed_successful_parallel_solvers'] = manifest.get('observed_successful_parallel_solvers', 0)
    report['updated_epoch'] = time.time()
    return report


def dispatch_analysis(app, manifest):
    app.repository(CANONICAL).repo('POST', '/actions/workflows/core-damage-analysis.yml/dispatches',
        {'ref': 'main', 'inputs': {'ticket': manifest['analysis_ticket'], 'source_sha': manifest['source_sha']}})


def reconcile_analysis(manifest, app):
    if manifest['phase'] != 'analysis_pending':
        return
    github = app.repository(CANONICAL)
    runs = github.repo('GET', '/actions/workflows/core-damage-analysis.yml/runs?event=workflow_dispatch&per_page=100')
    matches = [r for r in runs['workflow_runs']
               if r.get('display_title') == 'core-damage-analysis · ' + manifest['analysis_ticket']]
    if matches and matches[0]['status'] != 'completed':
        return
    if matches and manifest.get('analysis_attempt', 1) >= 3:
        manifest.update(phase='needs_investigation', error='Analysis failed three times; inspect its retained logs')
        return
    if matches or time.time() - manifest.get('analysis_dispatched_at', 0) > 900:
        manifest['analysis_attempt'] = manifest.get('analysis_attempt', 1) + 1
        manifest['analysis_dispatched_at'] = time.time()
        dispatch_analysis(app, manifest)


def fill_owner_slots(store, app, manifest, rows, capacity, owner):
    github = app.repository(owner + '/honeycomb-abaqus-studio')
    index = OWNERS.index(owner)
    owner_budget = capacity // 8 + (index < capacity % 8)
    if not owner_budget:
        return owner, {}
    reserved = sum(r['owner'] == owner and r['status'] == 'dispatching' and not r.get('workflow_run_id')
                   for r in manifest['designs'] + manifest['pilot_designs'])
    warnings = []
    slots = available_slots(owner_budget, page_jobs(github, warnings=warnings), reserved)
    pending = [r for r in rows if r['owner'] == owner
               and r['status'] in ('pending', 'checkpoint', 'solver_retry', 'preparation_retry')
               and admission(manifest, r)]
    dispatcher = Dispatcher(store, app)
    for row in pending[:slots]:
        dispatcher.reserve(manifest, row)
    return owner, {'scope': 'selected repository and visible public repositories; GitHub enforces shared owner quota',
                   'warnings': warnings}


def tick(store, app, identity):
    manifest = reconcile(store, store.load(identity), app)
    manifest = ingest_evidence(store, manifest, app)
    manifest = advance(manifest)
    manifest = ramp_capacity(manifest)
    reconcile_analysis(manifest, app)
    training_role = ('confirmation' if manifest['phase'] == 'confirmation' else
                     'adaptive' if manifest['phase'] == 'adaptive' else 'development')
    required = [r for r in manifest['designs'] if r['role'] in ((training_role, 'test') if manifest['phase'] == 'production' else (training_role,))]
    if manifest['phase'] in ('production', 'adaptive', 'confirmation') and required and all(r['status'] == 'completed' for r in required):
        manifest.update(analysis_origin_phase=manifest['phase'], phase='analysis_pending', analysis_ticket=uuid.uuid4().hex,
                        analysis_dispatched_at=time.time(), analysis_attempt=1)
        persist(store, manifest)
        dispatch_analysis(app, manifest)
    persist(store, manifest)
    capacity = dispatch_capacity(manifest)
    if manifest.get('enabled') and capacity:
        rows = dispatch_candidates(manifest)
        with ThreadPoolExecutor(max_workers=len(OWNERS)) as pool:
            observations = pool.map(lambda owner: fill_owner_slots(store, app, manifest, rows, capacity, owner), OWNERS)
            for owner, observation in observations:
                manifest.setdefault('quota_observation', {})[owner] = observation
    if manifest.get('quota_observation'):
        store.catalog.update(campaign_key(identity) + '/meta',
                             lambda old: {**old, 'quota_observation': manifest['quota_observation']})
    manifest = store.load(identity)
    report = status_report(manifest)
    store.catalog.update(campaign_key(identity) + '/status', lambda old: report)
    return report


def dispatch_candidates(manifest):
    """Overlap explicitly planned time/mesh checks with pilots under the same quota."""
    rows = runnable(manifest)
    if manifest.get('execution_scope') != 'full':
        return rows
    requested = set(manifest.get('prefetched_qualification_ids', []))
    existing = {r['id'] for r in rows}
    if manifest.get('phase') in ('pilot', 'time', 'mesh'):
        rows += [r for r in manifest['designs'] if r['id'] in requested and r['id'] not in existing
                 and r['role'] == 'qualification' and r.get('purpose') in ('time', 'mesh')]
    if (manifest.get('provisional_collection', {}).get('enabled') is True
            and manifest.get('phase') in ('pilot', 'time', 'sensitivity', 'development_time_audit')):
        existing = {r['id'] for r in rows}
        rows += [r for r in manifest['designs'] if r['id'] not in existing and r['role'] in ('development', 'test')]
    return rows


def relay(store, app, identity, workflow_run_id):
    """Keep the active campaign moving when GitHub's scheduled event is delayed."""
    metadata, _ = store.catalog.get(campaign_key(identity) + '/meta')
    active_phases = {'material', 'pilot', 'time', 'mesh', 'mesh_time', 'sensitivity',
                     'development_time_audit', 'production', 'adaptive', 'confirmation', 'analysis_pending'}
    if not metadata or not metadata.get('enabled') or metadata.get('phase') not in active_phases:
        return {'controller_redispatched': False}
    github = app.repository(CANONICAL)
    runs = github.repo('GET', '/actions/workflows/core-damage-controller.yml/runs?per_page=100')['workflow_runs']
    if any(r['status'] != 'completed' and str(r['id']) != str(workflow_run_id) for r in runs):
        return {'controller_redispatched': False, 'reason': 'Another controller is already queued'}
    github.repo('POST', '/actions/workflows/core-damage-controller.yml/dispatches', {'ref': 'main'})
    return {'controller_redispatched': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('initialize', 'tick', 'status', 'relay'))
    parser.add_argument('--campaign-id', default='core-damage-v2-20261106')
    args = parser.parse_args()
    app = CampaignApp()
    store = CampaignStore(app.repository(CANONICAL))
    if args.mode == 'relay':
        print(json.dumps(relay(store, app, args.campaign_id, os.environ['GITHUB_RUN_ID'])))
        return
    if args.mode == 'initialize':
        manifest = campaign_manifest(args.campaign_id)
        manifest['source_sha'] = os.environ['CAMPAIGN_SOURCE_SHA']
        from honeycomb_studio.cloud_runtime import execution_runtime
        from honeycomb_studio.models import digest
        runtime, errors = execution_runtime()
        if errors:
            raise ValueError('Pinned runtime unavailable')
        manifest['runtime_hash'] = digest(runtime)
        manifest['license_capacity'] = int(os.getenv('CORE_DAMAGE_MAX_SOLVERS', '0'))
        store.initialize(manifest)
    report = tick(store, app, args.campaign_id) if args.mode == 'tick' else status_report(store.load(args.campaign_id))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()

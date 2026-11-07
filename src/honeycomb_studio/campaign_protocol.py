"""Scientific phase transitions. Only actual, archived FE responses can open the next gate."""
from __future__ import annotations

from copy import deepcopy

import numpy as np

from .campaign import (
    BOUNDS,
    OWNERS,
    choose_velocity,
    freeze,
    numerical_gate,
    record,
    reference_model,
    response_converged,
)
from .models import ModelSpec, digest

PILOT_RESPONSE_ERROR = '8 m/s pilot did not meet the agreed response window'


def initial_designs(manifest):
    return [r for r in manifest['designs'] if r['role'] in ('development', 'test')]


def response_failure_collection_authorized(manifest):
    """A recorded decision permits raw collection of this exact initial plan only."""
    provisional = manifest.get('provisional_collection', {})
    decision = provisional.get('after_pilot_response_failure', {})
    rows = initial_designs(manifest)
    pilots = manifest['pilot_designs']
    return (manifest.get('execution_scope') == 'full' and provisional.get('enabled') is True
            and decision.get('enabled') is True and decision.get('failed_gate') == 'pilot_response_window'
            and manifest.get('mesh_policy', {}).get('mode') == 'reuse_actions_reference'
            and len(rows) == len({r['id'] for r in rows}) == 400
            and decision.get('initial_designs_hash') == digest({r['id']: r['physics_hash'] for r in rows})
            and len(pilots) == 3 and passed(pilots)
            and all(r.get('quality', {}).get('passed') is True and r.get('archive') for r in pilots)
            and all(r['model']['velocity_m_s'] == 8. and r['model']['duration'] == .008 for r in pilots)
            and choose_velocity(summaries(pilots)) is None)


def record_pilot_response_failure(manifest):
    manifest['scientific_block'] = {
        'gate': 'pilot_response_window', 'passed': False, 'error': PILOT_RESPONSE_ERROR,
        'pilot_responses': [{'id': r['id'], 'physics_hash': r['physics_hash'],
                             'workflow_run_id': r.get('workflow_run_id'),
                             'response': {k: r['summary'].get(k) for k in
                                          ('max_rear_downward_mm', 'rear_skin_deleted', 'event_finished')}}
                            for r in manifest['pilot_designs']]}


def qualification_designs(velocity, duration, *, mesh_factor=1., failure_displacement=.1, label="time"):
    rows = []
    for i, foil in enumerate((.035, .0635, .115)):
        model = reference_model(velocity_m_s=velocity, duration=duration, foil=foil,
                                field_intervals=round(duration / .00001),
                                history_intervals=round(duration / .000002))
        for key in ('core_centre_seed', 'core_outer_seed', 'skin_centre_seed', 'skin_outer_seed', 'reentrant_depth_seed'):
            setattr(model, key, getattr(model, key) * mesh_factor)
        for material in (model.core_material, model.face_material):
            assert material.johnson_cook is not None
            material.johnson_cook.failure_displacement_mm = failure_displacement
        row = record(model, 'qualification', i + 1, OWNERS[i], [])
        row['id'] = f'{label}-{round(duration * 1000):02d}ms-m{mesh_factor:g}-u{failure_displacement:g}-f{i}'
        row.update(purpose=label, mesh_factor=mesh_factor, failure_displacement_mm=failure_displacement)
        rows.append(row)
    return rows


def passed(rows):
    return bool(rows) and all(r['status'] == 'completed' and r.get('archive_verified')
                             and numerical_gate(r.get('summary', {}))['passed'] for r in rows)


def summaries(rows):
    return [r['summary'] for r in sorted(rows, key=lambda r: r['model']['foil'])]


def extend_unique(manifest, rows):
    known = {r['id'] for r in manifest['designs'] + manifest['pilot_designs']}
    counts = {owner: sum(r['owner'] == owner and r['status'] not in ('completed', 'retired')
                        and r['role'] in ('pilot', 'qualification')
                        for r in manifest['designs'] + manifest['pilot_designs']) for owner in OWNERS}
    for row in rows:
        if row['id'] in known:
            continue
        matches = [r for r in manifest['designs'] + manifest['pilot_designs']
                   if r['physics_hash'] == row['physics_hash'] and r['status'] == 'completed'
                   and r.get('archive_verified') and r.get('archive')
                   and numerical_gate(r.get('summary', {}))['passed']]
        if matches:
            source = matches[0]
            for key in ('owner', 'repository', 'status', 'summary', 'quality', 'archive', 'archive_verified',
                        'workflow_run_id', 'source_sha', 'execution_history'):
                if key in source:
                    row[key] = deepcopy(source[key])
            row['reused_from'] = {'design_id': source['id'], 'physics_hash': source['physics_hash'],
                                  'workflow_run_id': source.get('workflow_run_id')}
            manifest['designs'].append(row)
            known.add(row['id'])
            continue
        owner = min(OWNERS, key=lambda candidate: counts[candidate])
        row.update(owner=owner, repository=owner + '/honeycomb-abaqus-studio')
        manifest['designs'].append(row)
        known.add(row['id'])
        counts[owner] += 1


def audit_selection(manifest, count=40):
    rows = [r for r in manifest['designs'] if r['role'] == 'development']
    low, high = np.array(list(BOUNDS.values())).T
    x = (np.array([[r['model'][k] for k in BOUNDS] for r in rows]) - low) / (high - low)
    chosen = [0]
    distance = np.linalg.norm(x - x[0], axis=1)
    while len(chosen) < count:
        distance[chosen] = -1
        index = int(np.argmax(distance))
        chosen.append(index)
        distance = np.minimum(distance, np.linalg.norm(x - x[index], axis=1))
    return [rows[i]['id'] for i in chosen]


def advance(original):
    manifest = deepcopy(original)
    phase = manifest.get('phase', 'material')
    gates = manifest.setdefault('gates', {})
    if (phase == 'needs_investigation' and manifest.get('error') == PILOT_RESPONSE_ERROR
            and response_failure_collection_authorized(manifest)):
        record_pilot_response_failure(manifest)
        manifest.update(phase='provisional_collection', error=None)
        phase = manifest['phase']
    blocked = [r['id'] for r in runnable(manifest) if r['status'] in
               ('quarantined', 'budget_exhausted', 'needs_investigation')]
    if blocked:
        manifest.update(phase='needs_investigation', blocked_design_ids=blocked,
                        error='Required configurations exhausted their execution budget; inspect retained diagnostics')
        return manifest
    if phase == 'material':
        if gates.get('material', {}).get('passed') is True and gates.get('restart', {}).get('passed') is True:
            manifest['phase'] = 'pilot'
    elif phase == 'pilot':
        rows = manifest['pilot_designs']
        if not passed(rows):
            return manifest
        velocity = choose_velocity(summaries(rows))
        if manifest.get('execution_scope') == 'pilot':
            manifest.update(phase='pilot_completed', enabled=False,
                            pilot_report={'requested_velocities_m_s': sorted({r['model']['velocity_m_s'] for r in rows}),
                                          'duration_s': sorted({r['model']['duration'] for r in rows}),
                                          'selected_velocity_m_s': velocity,
                                          'response_window_passed': velocity is not None,
                                          'design_ids': [r['id'] for r in rows]})
            return manifest
        if velocity is None:
            record_pilot_response_failure(manifest)
            if response_failure_collection_authorized(manifest):
                manifest.update(phase='provisional_collection', error=None)
                return advance(manifest)
            else:
                manifest.update(phase='needs_investigation', error=PILOT_RESPONSE_ERROR)
        else:
            durations = {r['model']['duration'] for r in rows if r['model']['velocity_m_s'] == velocity}
            if len(durations) != 1:
                manifest.update(phase='needs_investigation', error='Selected pilot velocity has inconsistent durations')
                return manifest
            duration = durations.pop()
            if velocity != 8. or duration != .008:
                manifest.update(phase='needs_investigation', error='Pilot differs from the fixed 8 m/s, 8 ms contract')
                return manifest
            manifest.update(selected_velocity_m_s=velocity, trial_duration_s=duration, phase='time')
            extend_unique(manifest, qualification_designs(velocity, duration + .004))
    elif phase == 'provisional_collection':
        if not response_failure_collection_authorized(manifest):
            manifest.update(phase='needs_investigation', error='Fixed-plan provisional collection authorization no longer matches')
            return manifest
        rows = initial_designs(manifest)
        if passed(rows) and all(r.get('quality', {}).get('passed') is True and r.get('archive')
                                and not r.get('reused_from') for r in rows):
            manifest.update(phase='collection_completed', enabled=False,
                            collection_report={'completed_verified': len(rows),
                                               'initial_designs_hash': digest({r['id']: r['physics_hash'] for r in rows}),
                                               'global_qualification_passed': False,
                                               'scientific_block': deepcopy(manifest['scientific_block'])})
    elif phase == 'time':
        velocity, duration = manifest['selected_velocity_m_s'], manifest['trial_duration_s']
        short = [r for r in manifest['pilot_designs'] + manifest['designs']
                 if r['model']['velocity_m_s'] == velocity and r['model']['duration'] == duration
                 and (r['role'] == 'pilot' or r.get('purpose') == 'time')]
        long = qualification_designs(velocity, duration + .004)
        ids = {r['id'] for r in long}
        long = [r for r in manifest['designs'] if r['id'] in ids]
        if not passed(short) or not passed(long):
            return manifest
        if all(response_converged(a, b) for a, b in zip(summaries(short), summaries(long), strict=True)):
            gates['time'] = {'passed': True, 'short_ids': [r['id'] for r in short], 'long_ids': [r['id'] for r in long]}
            if manifest.get('mesh_policy', {}).get('mode') == 'reuse_actions_reference':
                gates['mesh'] = {'passed': True, 'method': 'user_selected_actions_reference',
                                 'convergence_verified': False, 'factor': 1.,
                                 'reference': deepcopy(manifest['mesh_policy'])}
                manifest.update(selected_duration_s=duration, selected_mesh_factor=1., phase='sensitivity')
                for displacement in (.05, .1, .2):
                    extend_unique(manifest, qualification_designs(velocity, duration,
                        mesh_factor=1., failure_displacement=displacement, label='sensitivity'))
                return manifest
            manifest.update(selected_duration_s=duration, phase='mesh')
            for factor in (1., .75, .5):
                extend_unique(manifest, qualification_designs(velocity, duration, mesh_factor=factor, label='mesh'))
        else:
            manifest.update(phase='needs_investigation', error='8 ms failed the 12 ms diagnostic comparison; review the protocol')
    elif phase == 'mesh':
        groups = [[r for r in manifest['designs'] if r.get('purpose') == 'mesh' and r['mesh_factor'] == factor]
                  for factor in (1., .75, .5)]
        if not all(passed(g) for g in groups):
            return manifest
        selected = None
        for factor, coarse, fine in zip((1., .75), groups[:-1], groups[1:], strict=True):
            if all(response_converged(a, b, .05) for a, b in zip(summaries(coarse), summaries(fine), strict=True)):
                selected = factor
                break
        if selected is None:
            manifest.update(phase='needs_investigation', error='Mesh convergence did not meet 5% gate')
        else:
            gates['mesh'] = {'passed': True, 'factor': selected, 'ids': [[r['id'] for r in g] for g in groups]}
            manifest.update(selected_mesh_factor=selected, phase='mesh_time')
            extend_unique(manifest, qualification_designs(manifest['selected_velocity_m_s'], .012,
                          mesh_factor=selected, label='mesh_time'))
    elif phase == 'mesh_time':
        selected = manifest['selected_mesh_factor']
        short = [r for r in manifest['designs'] if r.get('purpose') == 'mesh' and r['mesh_factor'] == selected]
        long = [r for r in manifest['designs'] if r.get('purpose') == 'mesh_time' and r['mesh_factor'] == selected]
        if len(short) != 3 or len(long) != 3 or not passed(short) or not passed(long):
            return manifest
        if not all(response_converged(a, b) for a, b in zip(summaries(short), summaries(long), strict=True)):
            manifest.update(phase='needs_investigation', error='Selected mesh failed the 8/12 ms diagnostic comparison')
            return manifest
        gates['time'] = {'passed': True, 'mesh_factor': selected,
                         'short_ids': [r['id'] for r in short], 'long_ids': [r['id'] for r in long]}
        manifest['phase'] = 'sensitivity'
        for displacement in (.05, .1, .2):
            extend_unique(manifest, qualification_designs(manifest['selected_velocity_m_s'], .008,
                mesh_factor=selected, failure_displacement=displacement, label='sensitivity'))
    elif phase == 'sensitivity':
        rows = [r for r in manifest['designs'] if r.get('purpose') == 'sensitivity']
        if len(rows) != 9 or not passed(rows):
            return manifest
        # Sensitivity is reported, never optimized to create a desired tear image.
        gates['damage_sensitivity'] = {'passed': True, 'ids': [r['id'] for r in rows],
            'responses': [{k: r['summary'].get(k) for k in ('SEA_core_J_kg', 'max_rear_downward_mm', 'rear_skin_deleted')}
                          for r in rows], 'calibrated': False}
        if gates.get('archive_restore', {}).get('passed') is True:
            manifest = freeze(manifest, manifest['selected_velocity_m_s'], manifest['selected_duration_s'],
                              manifest['selected_mesh_factor'], gates)
            manifest.update(audit_ids=audit_selection(manifest), phase='development_time_audit')
    elif phase == 'development_time_audit':
        short = [r for r in manifest['designs'] if r['id'] in manifest['audit_ids']]
        for row in short:
            model = ModelSpec.model_validate({**row['model'], 'duration': row['model']['duration'] + .004,
                                              'field_intervals': row['model']['field_intervals'] + 400,
                                              'history_intervals': row['model']['history_intervals'] + 2000})
            check = record(model, 'qualification', 1, row['owner'], [])
            check.update(id='audit-extension-' + row['id'], purpose='audit_extension', paired_id=row['id'])
            extend_unique(manifest, [check])
        long = [r for r in manifest['designs'] if r.get('purpose') == 'audit_extension']
        if not passed(short) or len(long) != 40 or not passed(long):
            return manifest
        by_id = {r['paired_id']: r for r in long}
        if all(response_converged(r['summary'], by_id[r['id']]['summary']) for r in short):
            gates['development_time_audit'] = {'passed': True, 'ids': manifest['audit_ids'],
                                               'extension_ids': [r['id'] for r in long]}
            manifest.update(phase='production', gates=gates)
        else:
            manifest.update(phase='needs_investigation', error='40-design common-duration audit failed')
    return manifest


def runnable(manifest):
    phase = manifest.get('phase')
    if manifest.get('execution_scope') == 'pilot' and phase != 'pilot':
        return []
    if phase == 'pilot':
        return manifest['pilot_designs']
    if phase in ('time', 'mesh', 'mesh_time', 'sensitivity'):
        return [r for r in manifest['designs'] if r.get('purpose') == phase]
    if phase == 'development_time_audit':
        return [r for r in manifest['designs'] if r['id'] in manifest['audit_ids'] or r.get('purpose') == 'audit_extension']
    if phase == 'production':
        return [r for r in manifest['designs'] if r['role'] in ('development', 'test')]
    if phase == 'provisional_collection' and response_failure_collection_authorized(manifest):
        return initial_designs(manifest)
    if phase in ('adaptive', 'confirmation'):
        return [r for r in manifest['designs'] if r['role'] == phase]
    return []

"""Consolidate all campaign configurations with explicit eligibility and provenance."""
import csv
import json
from pathlib import Path

from .models import ModelSpec


def flatten(value, prefix=''):
    result = {}
    for key, item in value.items():
        name = prefix + str(key)
        if isinstance(item, dict):
            result.update(flatten(item, name + '.'))
        elif isinstance(item, (tuple, list)):
            result[name] = json.dumps(item, ensure_ascii=False)
        else:
            result[name] = item
    return result


def export_campaign(manifest, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    index = []
    for row in manifest['designs'] + manifest['pilot_designs']:
        summary = row.get('summary', {})
        numerical = row.get('quality', {}).get('passed') is True
        training = (row['role'] in ('development', 'adaptive') and row.get('archive_verified') is True
                    and numerical and summary.get('rear_skin_deleted') is False
                    and summary.get('event_finished') is True)
        item = {'campaign_id': manifest['id'], 'design_id': row['id'], 'sample_group': row['role'],
                'varied_parameters': ','.join(row['varied_parameters']), 'repository': row['repository'],
                'physics_hash': row['physics_hash'], 'physical_inputs_hash': row['physical_inputs_hash'],
                'numerical_inputs_hash': row['numerical_inputs_hash'], 'source_sha': row.get('source_sha', manifest['source_sha']),
                'protocol_hash': manifest.get('protocol_hash'), 'runtime_hash': manifest.get('runtime_hash'),
                'status': row['status'], 'numerical_passed': numerical, 'ml_development_eligible': training,
                'archive_verified': row.get('archive_verified', False), 'attempt': row.get('attempt', 0),
                'segment': row.get('segment', 0), 'solver_error': row.get('error')}
        item.update(flatten(ModelSpec.model_validate(row['model']).resolved(), 'input.'))
        item['input.reentrant_indent'] = ModelSpec.model_validate(row['model']).effective_indent()
        item.update(flatten(summary, 'output.'))
        rows.append(item)
        index.append({k: row.get(k) for k in ('id', 'role', 'repository', 'physics_hash', 'archive',
                                             'execution_history', 'status')})
    columns = list(dict.fromkeys(k for row in rows for k in row))
    with (directory / 'samples.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, columns)
        writer.writeheader()
        writer.writerows(rows)
    for name, content in [('campaign-manifest.json', manifest), ('archive-index.json', index)]:
        (directory / name).write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf-8')
    units = {'SEA_core_J_kg': 'J/kg', 'max_rear_downward_mm': 'mm', 'core_mass_kg': 'kg',
             'velocity_m_s': 'm/s', 'duration': 's', 'reentrant_delta_deg': 'degree',
             'foil': 'mm', 'cell_size': 'mm', 'reentrant_cell_height': 'mm'}
    (directory / 'units-and-eligibility.json').write_text(json.dumps({
        'units': units, 'SEA_core': '(ALLPD_core + ALLDMD_core) / initial_core_mass_kg',
        'rear_displacement': 'maximum over connected rear nodes and all frames, relative to frame zero',
        'training': 'development/adaptive, archived, numerical gates passed, event complete, rear intact',
        'physical_validation': 'AA6061-T6 reference only; fracture evolution is uncalibrated',
        'test': 'held out until model locked; never used for adaptive sampling or model selection'}, indent=2), encoding='utf-8')
    return {'configurations': len(rows), 'development_eligible': sum(r['ml_development_eligible'] for r in rows),
            'numerical_passed': sum(r['numerical_passed'] for r in rows)}

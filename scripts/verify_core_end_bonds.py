"""Native regression for the eight immutable campaign inputs with clipped end caps."""
import hashlib
import json
import math
import os
import subprocess
from pathlib import Path

from native_cloud_worker import native_runtime

from honeycomb_studio.adapters.reentrant import network
from honeycomb_studio.campaign import campaign_manifest
from honeycomb_studio.cloud_github import Catalog, Github
from honeycomb_studio.cloud_runtime import execution_runtime
from honeycomb_studio.engine import Engine
from honeycomb_studio.models import BatchSpec, ModelSpec, digest
from honeycomb_studio.store import Store, write_json

BASE = 'f7d7e93cb0d97f8c99246a0a904d5d173a19a70d'
CASES = ('development-0084', 'development-0107', 'development-0118', 'development-0224',
         'development-0226', 'development-0242', 'development-0304', 'test-0029')
UNCHANGED = ('src/honeycomb_studio/models.py', 'src/honeycomb_studio/engine.py',
             'src/honeycomb_studio/solver.py', 'src/honeycomb_studio/data.py',
             'src/honeycomb_studio/cloud_continuation.py', 'scripts/campaign_worker.py',
             'scripts/native_cloud_worker.py', 'scripts/campaign_validate.py',
             'scripts/continuation_bundle.py', 'scripts/linux_media/continuation_control.c')


def main():
    components = {}
    for name in UNCHANGED:
        previous = subprocess.check_output(['git', 'show', BASE + ':' + name])
        current = Path(name).read_bytes()
        if previous != current:
            raise ValueError('Native repair cannot inherit changed solver/material/restart code: ' + name)
        components[name] = hashlib.sha256(current).hexdigest()
    root = Path('/tmp/honeycomb-end-bond-probe')
    root.mkdir()
    rows = {r['id']: r for r in campaign_manifest()['designs'] if r['id'] in CASES}
    if len(rows) != len(CASES):
        raise ValueError('Missing immutable regression inputs')
    reports = []
    with native_runtime(root, continuation=True) as executable:
        for case in CASES:
            original = ModelSpec.model_validate(rows[case]['model'])
            model = ModelSpec.model_validate({**original.model_dump(), 'duration': .00003,
                                              'field_intervals': 2, 'history_intervals': 30})
            store = Store(root / case)
            batch = store.create(BatchSpec(name=case, model=model, max_runs=1, data_profile='research',
                                          render_images=False, export_all_fields=False, backup_after_run=False))
            engine = Engine(store, abaqus=str(executable), fake=False)
            engine.explicit_continuation = True
            engine.continuation_control = Path('scripts/linux_media/continuation_control.c').resolve()
            engine.execute_wave(store.list('runs', batch['id']), batch)
            run = store.list('runs', batch['id'])[0]
            if run['status'] not in ('completed', 'review') or run.get('summary', {}).get('solver_completed') is not True:
                raise RuntimeError(case + ': ' + str(run.get('error')))
            raw = Path(run['attempts'][-1]['path']) / 'raw'
            actual = json.loads((raw / 'model_manifest.json').read_text())
            selected = actual['core_end_bond_selection']
            if not all(selected[end]['kind'] == 'end_cap_faces' and selected[end]['face_count'] > 0
                       and selected[end]['gap_mm'] <= original.tie_tolerance for end in ('TOP', 'BOTTOM')):
                raise ValueError('End-cap Tie regression failed: ' + case)
            resolved = original.resolved()
            walls = network(*(resolved[k] for k in ('panel', 'core_height', 'cell_size',
                            'reentrant_cell_height', 'reentrant_indent', 'phase_x', 'phase_z')),
                            min_length=resolved['min_segment_length'])
            volume = sum(math.dist(u, v) * resolved['panel'] * resolved['foil'] * (2 if double else 1)
                         for u, v, double in walls)
            expected_mass = volume * resolved['core_material']['density'] * 1.e-9
            if not math.isclose(actual['core_mass_kg'], expected_mass, rel_tol=1.e-7):
                raise ValueError('Core mass changed during the repair: ' + case)
            for dat in raw.glob('*.dat'):
                text = dat.read_text(errors='replace').lower()
                if 'warnnodemissmasterintersect' in text or 'have not been tied' in text:
                    raise ValueError('Untied core nodes in native preprocessing: ' + case)
            reports.append({'id': case, 'physical_inputs_hash': rows[case]['physical_inputs_hash'],
                            'passed': True, 'solver_completed': True, 'selection': selected,
                            'core_mass_kg': actual['core_mass_kg'], 'mass_preserved': True})
            write_json(Path('output/core-end-bond-progress.json'), {'completed': reports})
            store.db.close()
    runtime, errors = execution_runtime()
    if errors:
        raise ValueError('Runtime differs from the campaign')
    report = {'passed': True, 'source_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
              'base_source_sha': BASE, 'runtime_hash': digest(runtime),
              'workflow_run_id': int(os.environ['GITHUB_RUN_ID']), 'cases': reports,
              'diagnostic_duration_s': .00003, 'unchanged_native_components': components,
              'scope': 'core bonding construction; unchanged material, solver and restart components inherit base evidence'}
    write_json(Path('output/core-end-bond-verification.json'), report)
    Catalog(Github(token=os.environ['GITHUB_TOKEN'])).update('campaign-geometry-patch/latest', lambda _old: report)
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()

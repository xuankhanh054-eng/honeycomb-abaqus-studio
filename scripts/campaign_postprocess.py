"""Resume an immutable, completed ODB without ever admitting a new FE process."""
import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# The workflow checks out the ORIGINAL solver source at cwd and this recovery
# tool separately. Physics, extractor, and Engine imports remain the originals.
sys.path.insert(0, str(Path.cwd() / 'scripts'))

from campaign_worker import ROOT, publish_execution, source_sha
from cloud_worker import require_qualification
from native_cloud_worker import native_runtime

from honeycomb_studio import solver
from honeycomb_studio.actions_log import get_actions_log
from honeycomb_studio.campaign import numerical_gate, one_run_spec
from honeycomb_studio.campaign_queue import ticket_key
from honeycomb_studio.cloud_github import Catalog, Github
from honeycomb_studio.data import checksum
from honeycomb_studio.engine import Engine
from honeycomb_studio.models import BatchSpec, digest, expand
from honeycomb_studio.store import Store, write_json


class PostprocessOnlyEngine(Engine):
    def _monitor(self, r, a, phase, args, spool=None):
        if phase in ('building', 'checking', 'solving'):
            raise ValueError('A postprocess recovery must never start an FE process')
        return super()._monitor(r, a, phase, args, spool)


def prepare_completed_run(store, work):
    spec = BatchSpec.model_validate(one_run_spec(work['design']))
    expected = expand(spec)[0]
    rows = store.list('runs')
    if len(rows) != 1:
        raise ValueError('Recovery requires exactly one original run')
    run = rows[0]
    if (run['input_hash'] != expected['input_hash']
            or digest(run['resolved']) != work['design']['physics_hash']
            or run['resolved'] != expected['resolved'] or not run.get('attempts')):
        raise ValueError('Original run scientific inputs differ')
    attempt = run['attempts'][-1]
    directory = Path(attempt['path']).resolve()
    directory.relative_to(store.root)
    raw = directory / 'raw'
    odb = raw / (attempt['job_name'] + '.odb')
    odb.resolve().relative_to(raw.resolve())
    sta = solver.sta_progress(raw / (attempt['job_name'] + '.sta'), run['resolved']['duration'])
    if not sta.get('solver_success') or odb.is_symlink() or not odb.is_file() or not odb.stat().st_size:
        raise ValueError('No complete original native ODB; FE restart is forbidden in this workflow')
    # The independent STA success and final physical time are required even
    # when a portable solver-complete marker already exists.
    marker = directory / 'solver-complete.json'
    if not marker.exists():
        write_json(marker, {'sta': sta, 'verified_from_original_sta': True})
    attempt['process'] = None
    run.update(status='queued', explicit_recover=False, error=None)
    batch = store.get('batches', run['batch_id'])
    batch.update(status='running', pause_requested=False, stop_immediately=False, resource_error=None)
    store.save('runs', run)
    store.save('batches', batch)
    return run, batch, sta


def claim_recovery(old, workflow, original_workflow, original_source):
    if (not old or old.get('publication_kind') != 'postprocess'
            or old.get('publication_status') != 'ready' or old.get('publication_workflow_run_id')
            or old.get('workflow_run_id') != original_workflow or old.get('source_sha') != original_source):
        raise ValueError('Postprocess attempt has no exclusive original execution claim')
    return {**old, 'publication_status': 'claimed', 'publication_workflow_run_id': workflow}


def execute(work, github, catalog, key):
    original_source = source_sha()
    if work['source_sha'] != original_source or digest(require_qualification(Path.cwd())) != work['runtime_hash']:
        raise ValueError('Original source or pinned runtime differs')
    processor = subprocess.run(['git', '-C', str(Path(__file__).resolve().parents[1]), 'rev-parse', 'HEAD'],
                               check=True, capture_output=True, text=True).stdout.strip()
    if processor != os.environ['PROCESSOR_SOURCE_SHA']:
        raise ValueError('Postprocess tool source differs from its recorded revision')
    store = Store(ROOT)
    run, batch, sta = prepare_completed_run(store, work)
    attempt = run['attempts'][-1]
    odb = Path(attempt['path']) / 'raw' / (attempt['job_name'] + '.odb')
    original_digest = checksum(odb)
    original_attempts = len(run['attempts'])
    done = threading.Event()
    deadline = time.monotonic() + max(0, 300 * 60 -
        (time.time() - float(os.environ['HONEYCOMB_WORKFLOW_STARTED_EPOCH'])))

    def pulse():
        next_log = 0.
        while not done.is_set():
            if time.monotonic() >= deadline:
                current = store.get('batches', batch['id'])
                current.update(pause_requested=True, stop_immediately=True)
                store.save('batches', current)
                return
            if time.monotonic() >= next_log:
                try:
                    logger = get_actions_log()
                    if logger:
                        logger.runs(store.list('runs', batch['id']))
                        logger.resources()
                except Exception:
                    pass  # Optional console output cannot disable the local deadline.
                next_log = time.monotonic() + 60
            done.wait(5)

    reporter = threading.Thread(target=pulse, daemon=True)
    reporter.start()
    try:
        with native_runtime(ROOT, continuation=True) as executable:
            engine = PostprocessOnlyEngine(store, abaqus=str(executable), fake=False)
            engine.execute_wave([run], batch)
    finally:
        done.set()
        reporter.join(timeout=2)
    run = store.get('runs', run['id'])
    if run['status'] not in ('completed', 'review') or len(run['attempts']) != original_attempts:
        raise RuntimeError('Original ODB postprocessing did not complete; retained bytes must be inspected')
    if checksum(odb) != original_digest:
        raise ValueError('Original ODB bytes changed during recovery')
    result = {**work, 'outcome': 'completed', 'error': None, 'status': 'finished',
              'summary': run['summary'], 'quality': numerical_gate(run['summary']),
              'postprocess_source_sha': processor, 'postprocess_workflow_run_id': int(os.environ['GITHUB_RUN_ID']),
              'original_odb_sha256': original_digest, 'original_solver_evidence': sta,
              'new_fe_processes': 0, 'finished_at': time.time()}
    store.db.close()
    write_json(ROOT / 'campaign-execution.json', result)
    publish_execution(work, result, github, catalog, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('claim', 'run', 'failure'))
    args = parser.parse_args()
    github = Github(token=os.environ['GITHUB_TOKEN'])
    catalog = Catalog(github)
    key = ticket_key(os.environ['CAMPAIGN_ID'], os.environ['TICKET'])
    workflow = int(os.environ['GITHUB_RUN_ID'])
    if args.mode == 'claim':
        catalog.update(key, lambda old: claim_recovery(old, workflow, int(os.environ['ORIGINAL_RUN_ID']), source_sha()))
        return
    work, _ = catalog.get(key)
    if args.mode == 'failure':
        if work and work.get('publication_workflow_run_id') == workflow and ROOT.exists():
            catalog.update(key, lambda old: {**old, 'postprocess_artifact_run_id': workflow})
        return
    if not work or work.get('publication_workflow_run_id') != workflow:
        raise ValueError('Postprocess worker has no exclusive claim')
    execute(work, github, catalog, key)


if __name__ == '__main__':
    main()

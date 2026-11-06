"""One immutable design/segment per Actions job. Solver and publication retries are separate."""
import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from cloud_worker import require_qualification
from continuation_bundle import pack
from continuation_bundle import restore as restore_bundle
from native_cloud_worker import native_runtime

from honeycomb_studio import solver
from honeycomb_studio.campaign import numerical_gate, one_run_spec
from honeycomb_studio.campaign_queue import claim, receipt_hash, ticket_key
from honeycomb_studio.cloud_archive import ReleaseArchive
from honeycomb_studio.cloud_continuation import (
    checkpoint_inventory,
    verify_checkpoint,
    verify_checkpoint_history,
)
from honeycomb_studio.cloud_github import Catalog, Github
from honeycomb_studio.cloud_progress import Progress
from honeycomb_studio.cloud_release import reserve_release
from honeycomb_studio.cloud_restore import restore
from honeycomb_studio.engine import Engine
from honeycomb_studio.models import BatchSpec, digest, expand
from honeycomb_studio.store import Store, write_json

ROOT = Path('/tmp/honeycomb-results')


class TicketProgress:
    def __init__(self, catalog, key):
        self.catalog, self.key = catalog, key

    def get(self, _key):
        return self.catalog.get(self.key)

    def update(self, _key, transform):
        return self.catalog.update(self.key, transform)


def source_sha():
    return subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, text=True, capture_output=True).stdout.strip()


def execute(work, github, catalog, key):
    runtime_hash = digest(require_qualification(Path.cwd()))
    if runtime_hash != work['runtime_hash']:
        raise ValueError('Pinned runtime fingerprint differs')
    design = work['design']
    if digest(design['model']) != digest(BatchSpec.model_validate(one_run_spec(design)).model.model_dump()):
        raise ValueError('Invalid scientific contract')
    spec = BatchSpec.model_validate(one_run_spec(design))
    expected = expand(spec)[0]
    if digest(spec.model.resolved()) != design['physics_hash']:
        raise ValueError('Design physics hash differs')
    segment = work['segment']
    if segment:
        predecessor = work['predecessor']
        if predecessor.get('receipt_hash') != receipt_hash(predecessor):
            raise ValueError('Predecessor receipt checksum differs')
        archive = predecessor['archive']
        if digest(archive) != predecessor['archive_sha256']:
            raise ValueError('Predecessor archive checksum differs')
        with tempfile.TemporaryDirectory(prefix='campaign-restore-') as temp:
            restore(archive, Path(temp), allow_incomplete=True)
            restore_bundle(Path(temp), segment)
    elif ROOT.exists():
        raise ValueError('First segment requires clean storage')
    store = Store(ROOT)
    marker = ROOT / 'continuation.json'
    if segment:
        receipt = json.loads(marker.read_text())
        if (receipt['ticket'] != work['predecessor']['ticket'] or receipt['segment'] != segment - 1
                or receipt['source_sha'] != work['source_sha'] or receipt['physics_hash'] != design['physics_hash']):
            raise ValueError('Wrong predecessor runner or design')
        batch = store.get('batches', receipt['batch_id'])
        run = store.get('runs', receipt['run_id'])
        attempt = run['attempts'][-1]
        raw = Path(attempt['path']) / 'raw'
        verify_checkpoint(raw, receipt, job_name=attempt['job_name'], input_hash=expected['input_hash'],
                          runtime_hash=runtime_hash, cpus=4)
        if run['resolved'] != expected['resolved'] or run['input_hash'] != expected['input_hash']:
            raise ValueError('Restored model differs from campaign')
        if not (raw / 'continuation-stop-ack.json').is_file():
            raise ValueError('Unacknowledged Explicit checkpoint')
        for lock in raw.glob(attempt['job_name'] + '*.lck'):
            lock.unlink()
        attempt['process'] = None
        run.update(status='queued', explicit_recover=True, phase='queued', error=None)
        store.save('runs', run)
    else:
        batch = store.create(spec)
        run = store.list('runs', batch['id'])[0]
    batch.update(status='running', pause_requested=False, stop_immediately=False)
    store.save('batches', batch)
    control = Path('scripts/linux_media/continuation_control.c').resolve()
    deadline = time.monotonic() + max(0, 240 * 60 - (time.time() - float(os.environ['HONEYCOMB_WORKFLOW_STARTED_EPOCH'])))
    progress = Progress(github, TicketProgress(catalog, key), work['ticket'], store, batch['id'], deadline)
    history_check = None
    with native_runtime(ROOT, continuation=True) as executable:
        release_info = subprocess.run(solver.command(str(executable), 'information=release'), cwd=ROOT,
                                      check=True, text=True, capture_output=True, timeout=90)
        if '2020' not in release_info.stdout:
            raise ValueError('Installed runtime release differs')
        engine = Engine(store, abaqus=str(executable), fake=False)
        engine.explicit_continuation = True
        engine.continuation_control = control
        engine.runtime_fingerprint = runtime_hash
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: progress.stop_solver('cancelled'))
        progress.start(work['source_sha'])
        solver_started_at = time.time()
        catalog.update(key, lambda old: {**old, 'solver_started_at': solver_started_at})
        try:
            engine.execute_wave([run], batch)
        finally:
            solver_finished_at = time.time()
            status = store.get('runs', run['id'])['status']
            conclusion = ('timed_out' if progress.stop_reason == 'timed_out' else
                          status if status in ('completed', 'review') else 'failed')
            progress.finish(conclusion)
        run = store.get('runs', run['id'])
        if progress.stop_reason == 'timed_out' and run['status'] == 'interrupted':
            attempt = run['attempts'][-1]
            raw = Path(attempt['path']) / 'raw'
            history_check = verify_checkpoint_history(raw, attempt['job_name'], str(executable), run['resolved'])
    run = store.get('runs', run['id'])
    attempt = run['attempts'][-1] if run['attempts'] else {}
    solving_start = attempt.get('phase_started_at', {}).get('solving')
    solving_end = (solving_start + attempt.get('durations', {}).get('solving', solver_finished_at - solving_start)
                   if solving_start else None)
    outcome = 'completed' if run['status'] in ('completed', 'review') else 'solver_failed'
    resource_error = store.get('batches', batch['id']).get('resource_error')
    if not run['attempts'] and resource_error:
        outcome = 'prepare_failed'
    receipt = None
    if progress.stop_reason == 'timed_out' and run['status'] == 'interrupted':
        attempt = run['attempts'][-1]
        raw = Path(attempt['path']) / 'raw'
        acknowledgement = raw / 'continuation-stop-ack.json'
        if not acknowledgement.is_file():
            raise ValueError('Solver did not acknowledge an orderly restart checkpoint')
        if history_check is None:
            raise ValueError('Original checkpoint history was not verified in the native runtime')
        receipt = {'ticket': work['ticket'], 'segment': segment, 'source_sha': work['source_sha'],
                   'physics_hash': design['physics_hash'], 'batch_id': batch['id'], 'run_id': run['id'],
                   'job_name': attempt['job_name'], 'input_hash': run['input_hash'],
                   'runtime_hash': runtime_hash, 'cpus': 4,
                   'files': checkpoint_inventory(raw, attempt['job_name']),
                   'orderly_checkpoint': history_check,
                   'stop_acknowledgement': json.loads(acknowledgement.read_text())}
        write_json(marker, receipt)
        outcome = 'checkpoint'
    store.db.close()  # flush SQLite before sealing the cross-runner state
    result = {**work, 'outcome': outcome, 'error': run.get('error') or resource_error, 'finished_at': time.time(),
              'solver_started_at': solving_start, 'solver_finished_at': solving_end,
              'solver_wall_seconds': solving_end - solving_start if solving_start else None,
              'execution_wall_seconds': solver_finished_at - float(os.environ['HONEYCOMB_WORKFLOW_STARTED_EPOCH'])}
    if outcome in ('solver_failed', 'prepare_failed'):
        catalog.update(key, lambda old: {**old, **result, 'status': 'finished'})
        raise RuntimeError(outcome + '; inspect retained diagnostics: ' + str(result['error']))
    if outcome == 'completed':
        result.update(summary=run['summary'], quality=numerical_gate(run['summary']))
    write_json(ROOT / 'campaign-execution.json', result)
    publish_execution(work, result, github, catalog, key)


def publish_execution(work, result, github, catalog, key):
    """Idempotent upload of already computed bytes; this function never starts Abaqus."""
    tag = 'core-damage-' + work['ticket']
    try:
        source_tag = 'core-damage-source-' + work['source_sha'][:12]
        pinned = github.repo('GET', '/git/ref/tags/' + source_tag, allow_missing=True)
        if pinned is None:
            github.repo('POST', '/git/refs', {'ref': 'refs/tags/' + source_tag, 'sha': work['source_sha']})
        release = reserve_release(github, tag=tag, sha=work['source_sha'], name=work['design']['id'], source_tag=source_tag)
        publisher = ReleaseArchive(github, release['id'], tag=tag)
        if result['outcome'] == 'checkpoint':
            receipt = json.loads((ROOT / 'continuation.json').read_text())
            with tempfile.TemporaryDirectory(prefix='campaign-state-', dir=ROOT.parent) as temp:
                pack(Path(temp), 'checkpoint')
                archive = publisher.publish(Path(temp), attempt=work['ticket'], complete=False)
            receipt.update(outcome='checkpoint', archive=archive, archive_sha256=digest(archive))
            receipt['receipt_hash'] = receipt_hash(receipt)
            result['receipt'] = receipt
        else:
            archive = publisher.publish(ROOT, attempt=work['ticket'], complete=True)
        release = github.repo('PATCH', '/releases/' + str(release['id']), {'draft': False})
        result.update(archive_verified=True, archive=archive, release_url=release['html_url'], status='finished')
        write_json(ROOT / 'publication-result.json', result)
        catalog.update(key, lambda old: {**old, **result})
    except Exception as exc:
        # The workflow's immutable fallback artifact retains the already computed ODB/state.
        write_json(ROOT / 'publication-pending.json', {**result, 'publication_error': str(exc),
                   'release_tag': tag})
        error = str(exc)
        catalog.update(key, lambda old: {**old, 'outcome': 'upload_failed', 'status': 'finished',
                                        'recoverable_artifact': 'core-damage-' + work['ticket'], 'error': error})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('claim', 'run', 'failure'))
    parser.add_argument('--campaign-id', required=True)
    parser.add_argument('--ticket', required=True)
    args = parser.parse_args()
    github = Github(token=os.environ['GITHUB_TOKEN'])
    catalog = Catalog(github)
    key = ticket_key(args.campaign_id, args.ticket)
    workflow = int(os.environ['GITHUB_RUN_ID'])
    if args.mode == 'claim':
        catalog.update(key, lambda old: claim(old, ticket=args.ticket, workflow=workflow,
                                             source_sha=source_sha(), repository=github.repository))
        return
    work, _ = catalog.get(key)
    if args.mode == 'failure':
        if work and work.get('workflow_run_id') == workflow and work.get('status') == 'claimed':
            outcome = 'solver_failed' if work.get('solver_started_at') else 'prepare_failed'
            catalog.update(key, lambda old: {**old, 'outcome': outcome, 'status': 'finished',
                'error': 'Workflow stopped before a verified execution receipt; inspect Actions diagnostics'})
        return
    if not work or work.get('workflow_run_id') != workflow or work['status'] != 'claimed':
        raise ValueError('Worker has no exclusive execution claim')
    if work['source_sha'] != source_sha():
        raise ValueError('Source SHA differs from claimed code')
    execute(work, github, catalog, key)


if __name__ == '__main__':
    main()

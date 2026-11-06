import ast
import importlib.util
import io
import json
import tarfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from honeycomb_studio.cloud_continuation import checkpoint_inventory, verify_checkpoint
from honeycomb_studio.engine import Engine
from honeycomb_studio.models import BatchSpec
from honeycomb_studio.store import Store


def raw_files(tmp_path):
    for ext in ('abq', 'pac', 'res', 'sel', 'odb'):
        (tmp_path / ('hc.' + ext)).write_bytes(ext.encode())
    return checkpoint_inventory(tmp_path, 'hc')


def test_recovery_reader_skips_absent_abaqus_fields(tmp_path, monkeypatch):
    import sys

    class Repository(dict):
        def __contains__(self, name):
            if not dict.__contains__(self, name):
                raise KeyError(name)
            return True

    fields = Repository(U=SimpleNamespace(values=[SimpleNamespace(dataDouble=(1., 2., 3.))]))
    frame = SimpleNamespace(frameValue=.02, fieldOutputs=fields)
    odb = SimpleNamespace(steps={'Test': SimpleNamespace(historyRegions={}, frames=[frame])}, close=lambda: None)
    monkeypatch.setitem(sys.modules, 'odbAccess', SimpleNamespace(openOdb=lambda *args, **kwargs: odb))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['reader.py', str(tmp_path / 'history.json')])
    path = Path(__file__).parents[1] / 'scripts/verify_explicit_recover.py'
    source = next(node.value for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and node.value.startswith('from odbAccess import openOdb'))
    exec(compile(source, 'native-recovery-reader', 'exec'), {})
    states = json.loads((tmp_path / 'history.json.state.json').read_text())
    assert states == [{'time': .02, 'fields': {'U': [[1., 2., 3.]]}}]


def test_recovery_rejects_corruption_or_different_input(tmp_path):
    files = raw_files(tmp_path)
    receipt = dict(job_name='hc', input_hash='input', runtime_hash='runtime', cpus=4, files=files)
    verify_checkpoint(tmp_path, receipt, job_name='hc', input_hash='input', runtime_hash='runtime', cpus=4)
    with pytest.raises(ValueError, match='identity'):
        verify_checkpoint(tmp_path, receipt, job_name='hc', input_hash='other', runtime_hash='runtime', cpus=4)
    (tmp_path / 'hc.abq').write_bytes(b'broken')
    with pytest.raises(ValueError, match='checksum'):
        verify_checkpoint(tmp_path, receipt, job_name='hc', input_hash='input', runtime_hash='runtime', cpus=4)


def test_recovery_requires_all_state_families(tmp_path):
    raw_files(tmp_path)
    (tmp_path / 'hc.sel').unlink()
    with pytest.raises(ValueError, match='sel'):
        checkpoint_inventory(tmp_path, 'hc')


@pytest.mark.parametrize('gap', [False, True])
def test_checkpoint_must_contain_original_history_through_stop_time(tmp_path, monkeypatch, gap):
    from honeycomb_studio.cloud_continuation import verify_checkpoint_history
    (tmp_path / 'continuation-stop-ack.json').write_text(json.dumps({'simulation_time_s': .001}))
    times = [i * .000005 for i in range(201) if not gap or not 100 <= i < 120]
    history = [{'step': 'Impact', 'region': 'IMPACTOR', 'variable': 'A3', 'data': [[t, 0] for t in times]}]
    def read(args, **kwargs):
        Path(args[-1]).write_text(json.dumps(history))
    monkeypatch.setattr('honeycomb_studio.cloud_continuation.subprocess.run', read)
    spec = {'duration': .012, 'history_intervals': 2400}
    if gap:
        with pytest.raises(ValueError, match='missing original history'):
            verify_checkpoint_history(tmp_path, 'hc', 'fixture', spec)
    else:
        assert verify_checkpoint_history(tmp_path, 'hc', 'fixture', spec)['history_integrity']['passed']


def test_segment_reads_checkpoint_before_native_runtime_closes(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
    import cloud_segment
    active = []
    run = {'id': 'run', 'status': 'interrupted', 'resolved': {'duration': .012},
           'attempts': [{'path': str(tmp_path), 'job_name': 'hc'}]}
    @contextmanager
    def runtime(root, **kwargs):
        active.append(True)
        try:
            yield Path('native-wrapper')
        finally:
            active.pop()
    def read(*args):
        assert active, 'private native runtime was closed before querying the ODB'
        return {'history_integrity': {'passed': True}}
    engine = SimpleNamespace(execute_wave=lambda *args: None)
    progress = SimpleNamespace(stop_reason='timed_out', stop_solver=lambda *args: None,
                               start=lambda *args: None, done=SimpleNamespace(set=lambda: None),
                               thread=SimpleNamespace(join=lambda **kwargs: None))
    monkeypatch.setattr(cloud_segment, 'native_runtime', runtime)
    monkeypatch.setattr(cloud_segment, 'Engine', lambda *args, **kwargs: engine)
    monkeypatch.setattr(cloud_segment, 'verify_checkpoint_history', read)
    monkeypatch.setattr(cloud_segment.signal, 'signal', lambda *args: None)
    monkeypatch.setenv('GITHUB_SHA', 'revision')
    store = SimpleNamespace(get=lambda *args: run)
    actual, check = cloud_segment.execute_segment(tmp_path, store, run, {}, progress, 'runtime', tmp_path / 'control.c')
    assert actual == run and check['history_integrity']['passed']
    assert not active


@pytest.mark.parametrize('resource_blocked', [False, True])
def test_campaign_reads_original_checkpoint_history_inside_native_runtime(tmp_path, monkeypatch, resource_blocked):
    import sys
    import time
    sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
    import campaign_worker

    from honeycomb_studio.campaign import campaign_manifest
    from honeycomb_studio.models import digest

    root = tmp_path / 'results'
    active, checked, published = [], [], []

    @contextmanager
    def runtime(*args, **kwargs):
        active.append(True)
        try:
            yield Path('native-wrapper')
        finally:
            active.pop()

    class FakeEngine:
        def __init__(self, store, **kwargs):
            self.store = store

        def execute_wave(self, runs, batch):
            if resource_blocked:
                batch.update(status='paused', resource_error='Disk fixture: insufficient space')
                self.store.save('batches', batch)
                return
            run = runs[0]
            folder = root / 'attempt'
            raw = folder / 'raw'
            raw.mkdir(parents=True)
            (raw / 'continuation-stop-ack.json').write_text('{"simulation_time_s":0.002}')
            for extension in ('abq', 'pac', 'res', 'sel', 'odb'):
                (raw / ('hc.' + extension)).write_bytes(b'native-state')
            run.update(status='interrupted', attempts=[dict(path=str(folder), job_name='hc',
                phase_started_at={'solving': time.time() - 1}, durations={'solving': 1})])
            self.store.save('runs', run)

    def read(*args):
        assert active, 'runtime closed before querying original campaign history'
        checked.append(True)
        return {'history_integrity': {'passed': True}}

    progress = SimpleNamespace(stop_reason='timed_out', start=lambda *args: None, finish=lambda *args: None)
    monkeypatch.setattr(campaign_worker, 'ROOT', root)
    monkeypatch.setattr(campaign_worker, 'native_runtime', runtime)
    monkeypatch.setattr(campaign_worker, 'Engine', FakeEngine)
    monkeypatch.setattr(campaign_worker, 'Progress', lambda *args: progress)
    monkeypatch.setattr(campaign_worker, 'require_qualification', lambda *args: {})
    monkeypatch.setattr(campaign_worker, 'verify_checkpoint_history', read, raising=False)
    monkeypatch.setattr(campaign_worker.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(campaign_worker.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout='2020'))
    monkeypatch.setattr(campaign_worker, 'publish_execution', lambda work, result, *args: published.append(result))
    monkeypatch.setenv('HONEYCOMB_WORKFLOW_STARTED_EPOCH', str(time.time()))
    work = dict(runtime_hash=digest({}), design=campaign_manifest()['pilot_designs'][0], segment=0,
                ticket='a' * 32, source_sha='b' * 40)
    catalog = SimpleNamespace(update=lambda key, transform: transform(work))
    if resource_blocked:
        with pytest.raises(RuntimeError, match='prepare_failed.*Disk fixture'):
            campaign_worker.execute(work, None, catalog, 'ticket')
        assert not checked and not published and not active
        return
    campaign_worker.execute(work, None, catalog, 'ticket')
    assert checked and not active
    assert published[0]['outcome'] == 'checkpoint'
    receipt = json.loads((root / 'continuation.json').read_text())
    assert receipt['orderly_checkpoint']['history_integrity']['passed']


@pytest.mark.parametrize('error', ['munmap_chunk(): invalid pointer', 'free(): corrupted unsorted chunks',
    'double free or corruption (out)', 'corrupted double-linked list', 'rank 0 terminated by signal 11'])
def test_native_cleanup_crash_cannot_be_accepted_as_an_orderly_stop(tmp_path, error):
    from honeycomb_studio.cloud_continuation import verify_solver_cleanup
    log = tmp_path / 'solver.log'
    log.write_text('Restart written\nThe program will exit as per user request in VEXTERNALDB\n' + error)
    with pytest.raises(RuntimeError, match='cleanup'):
        verify_solver_cleanup(log)
    log.write_text('Restart written\nThe program will exit as per user request in VEXTERNALDB\n')
    verify_solver_cleanup(log)


def test_engine_continues_original_attempt_without_building(monkeypatch, tmp_path):
    store = Store(tmp_path)
    batch = store.create(BatchSpec(name='recover'))
    run = store.list('runs', batch['id'])[0]
    attempt_path = Path(run['path']) / 'attempts/001'
    (attempt_path / 'raw').mkdir(parents=True)
    (attempt_path / 'logs').mkdir()
    run['attempts'] = [{'number': 1, 'path': str(attempt_path), 'job_name': 'original'}]
    run['explicit_recover'] = True
    engine = Engine(store, fake=False)
    calls = []
    def monitor(r, a, phase, args):
        assert phase == 'solving' and 'recover' in args
        assert 'job=original' in args and not any(v.startswith('input=') for v in args)
        calls.append(args)
        (attempt_path / 'raw/original.sta').write_text(
            ' 101338 6.000E-03 6.000E-03 00:23:48 5.704E-08 26813 3.013E+02 7.002E+03\n'
            'THE ANALYSIS HAS COMPLETED SUCCESSFULLY')
    monkeypatch.setattr(engine, '_monitor', monitor)
    monkeypatch.setattr(engine, '_postprocess', lambda *args: None)
    engine._execute(run, batch)
    assert len(calls) == 1 and len(run['attempts']) == 1
    assert (attempt_path / 'solver-complete.json').is_file()
    store.db.close()


def bundle_module():
    spec = importlib.util.spec_from_file_location('continuation_bundle', Path(__file__).parents[1] / 'scripts/continuation_bundle.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_linked_checkpoint_admission_budgets_one_odb_and_bounded_upload_part():
    from honeycomb_studio.campaign import one_run_spec, pilots
    from honeycomb_studio.campaign_protocol import qualification_designs
    from honeycomb_studio.execution import execution_plan
    from honeycomb_studio.models import ModelSpec

    design = pilots()[0]
    spec = one_run_spec(design)
    model = ModelSpec.model_validate(design['model']).resolved()
    assert execution_plan(spec, [model], 4, 16, 100)['effective_parallel_runs'] == 0
    assert execution_plan(spec, [model], 4, 16, 100, linked_checkpoint=True)['effective_parallel_runs'] == 1
    fine = qualification_designs(8., .008, mesh_factor=.5)[0]
    model = ModelSpec.model_validate(fine['model']).resolved()
    assert execution_plan(spec, [model], 4, 16, 145, linked_checkpoint=True)['effective_parallel_runs'] == 1
    assert execution_plan(spec, [model], 4, 16, 100, linked_checkpoint=True)['effective_parallel_runs'] == 0
    assert execution_plan(spec, [model], 4, 16, 60, linked_checkpoint=True,
                          retained_disk_gb=[85])['effective_parallel_runs'] == 1
    assert execution_plan(spec, [model], 4, 16, 20, linked_checkpoint=True,
                          retained_disk_gb=[85])['effective_parallel_runs'] == 0
    spec['export_all_fields'] = True
    assert execution_plan(spec, [model], 4, 16, 145, linked_checkpoint=True)['effective_parallel_runs'] == 0


def test_segment_bundle_roundtrip_and_tamper(monkeypatch, tmp_path):
    mod = bundle_module()
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'state').write_bytes(b'checkpoint')
    monkeypatch.setattr(mod, 'ROOT', root)
    folder = tmp_path / 'bundle'
    mod.pack(folder, 'checkpoint')
    (root / 'state').unlink()
    root.rmdir()
    assert mod.restore(folder, 1) is False
    assert (root / 'state').read_bytes() == b'checkpoint'
    (root / 'state').unlink()
    root.rmdir()
    (folder / 'state' / 'state').write_bytes(b'broken')
    with pytest.raises(ValueError, match='Corrupt'):
        mod.restore(folder, 1)


def test_checkpoint_directory_preserves_large_payload_without_a_second_copy(tmp_path):
    mod = bundle_module()
    source = tmp_path / 'source'
    source.mkdir()
    payload = source / 'job.abq.1'
    payload.write_bytes(b'large native state')
    (source / 'catalog.sqlite').write_bytes(b'catalog')
    bundle = tmp_path / 'bundle'
    mod.pack(bundle, 'checkpoint', root=source)
    assert not (bundle / 'state.tar').exists()
    assert (bundle / 'state/job.abq.1').samefile(payload)
    restored = tmp_path / 'restored'
    mod.restore(bundle, 1, root=restored)
    assert (restored / 'job.abq.1').samefile(payload)
    assert (restored / 'catalog.sqlite').read_bytes() == b'catalog'


@pytest.mark.parametrize('name', ['../escape', '/escape', 'C:/escape', 'a\\escape'])
def test_checkpoint_directory_rejects_unsafe_receipt_paths(tmp_path, name):
    mod = bundle_module()
    (tmp_path / 'segment.json').write_text(json.dumps({
        'outcome': 'checkpoint', 'format': 'directory-v1', 'files': {name: {}}}))
    with pytest.raises(ValueError, match='Unsafe'):
        mod.restore(tmp_path, 1, root=tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_bundle_rejects_traversal(monkeypatch, tmp_path):
    import hashlib
    mod = bundle_module()
    monkeypatch.setattr(mod, 'ROOT', tmp_path / 'root')
    archive = tmp_path / 'state.tar'
    with tarfile.open(archive, 'w') as tar:
        member = tarfile.TarInfo('../escape')
        member.size = 1
        tar.addfile(member, io.BytesIO(b'x'))
    (tmp_path / 'segment.json').write_text(json.dumps({'outcome': 'checkpoint', 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'bytes': archive.stat().st_size}))
    with pytest.raises(ValueError, match='Unsafe'):
        mod.restore(tmp_path, 1)
    assert not (tmp_path / 'escape').exists()

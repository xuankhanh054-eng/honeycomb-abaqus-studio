import importlib.util
import sys
from pathlib import Path

import pytest

from honeycomb_studio.campaign import one_run_spec, record, reference_model
from honeycomb_studio.models import BatchSpec, expand
from honeycomb_studio.store import Store


def tool():
    scripts = Path(__file__).parents[1] / 'scripts'
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location('campaign_postprocess_test', scripts / 'campaign_postprocess.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path):
    design = record(reference_model(), 'development', 1, 'xuankhanh24', [])
    spec = BatchSpec.model_validate(one_run_spec(design))
    store = Store(tmp_path)
    batch = store.create(spec)
    run = store.list('runs', batch['id'])[0]
    attempt = Path(run['path']) / 'attempts' / '001'
    raw = attempt / 'raw'
    raw.mkdir(parents=True)
    (raw / 'hc.odb').write_bytes(b'unchanged completed native ODB')
    (raw / 'hc.sta').write_text(
        ' 101338 8.000E-03 8.000E-03 00:23:48 5.704E-08 26813 3.013E+02 7.002E+03\n'
        ' THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n')
    run.update(status='interrupted', attempts=[{'path': str(attempt), 'job_name': 'hc', 'process': None}])
    store.save('runs', run)
    return store, design, run, expand(spec)[0]


def test_completed_solver_without_restart_ack_can_resume_only_postprocessing(tmp_path):
    module = tool()
    store, design, run, _ = fixture(tmp_path)
    assert not (Path(run['attempts'][0]['path']) / 'raw' / 'continuation-stop-ack.json').exists()
    actual, batch, evidence = module.prepare_completed_run(store, {'design': design})
    assert actual['id'] == run['id'] and actual['status'] == 'queued'
    assert len(actual['attempts']) == 1 and not actual.get('explicit_recover')
    assert evidence['solver_success'] is True
    assert batch['pause_requested'] is False


@pytest.mark.parametrize('problem', ['short_analysis', 'different_input', 'missing_odb'])
def test_postprocess_rejects_partial_or_different_solver_bytes(tmp_path, problem):
    module = tool()
    store, design, run, _ = fixture(tmp_path)
    raw = Path(run['attempts'][0]['path']) / 'raw'
    if problem == 'short_analysis':
        text = (raw / 'hc.sta').read_text().replace('8.000E-03', '4.000E-03')
        (raw / 'hc.sta').write_text(text)
    elif problem == 'different_input':
        run['input_hash'] = 'wrong'
        store.save('runs', run)
    else:
        (raw / 'hc.odb').unlink()
    with pytest.raises(ValueError):
        module.prepare_completed_run(store, {'design': design})


def test_postprocess_engine_refuses_any_new_fe_process(tmp_path):
    module = tool()
    store, _, _, _ = fixture(tmp_path)
    engine = module.PostprocessOnlyEngine(store, abaqus='never-launch', fake=False)
    for phase in ('building', 'checking', 'solving'):
        with pytest.raises(ValueError, match='FE process'):
            engine._monitor({}, {}, phase, ['forbidden'])


def test_recovery_uses_the_original_attempt_and_odb(tmp_path, monkeypatch):
    module = tool()
    store, design, _, _ = fixture(tmp_path)
    run, batch, _ = module.prepare_completed_run(store, {'design': design})
    engine = module.PostprocessOnlyEngine(store, abaqus='must-not-run-fe', fake=False)
    recovered = []
    monkeypatch.setattr(engine, '_postprocess', lambda r, attempt, b: recovered.append(attempt['path']))
    engine._execute(run, batch)
    assert recovered == [run['attempts'][0]['path']]
    assert len(store.get('runs', run['id'])['attempts']) == 1
    assert (Path(recovered[0]) / 'raw' / 'hc.odb').read_bytes() == b'unchanged completed native ODB'


def test_postprocess_wave_does_not_reserve_space_for_a_second_native_odb(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import honeycomb_studio.engine as orchestration
    from honeycomb_studio.execution import disk_requirement

    module = tool()
    store, design, _, _ = fixture(tmp_path)
    run, batch, _ = module.prepare_completed_run(store, {'design': design})
    linked = disk_requirement(batch['spec'], run['resolved'], linked_checkpoint=True)
    full = disk_requirement(batch['spec'], run['resolved'])
    assert linked < full
    available = batch['spec']['min_free_gb'] + (linked + full) / 2
    monkeypatch.setattr(orchestration.psutil, 'cpu_count', lambda: 4)
    monkeypatch.setattr(orchestration.psutil, 'virtual_memory', lambda: SimpleNamespace(available=16 * 1024**3))
    monkeypatch.setattr(orchestration.shutil, 'disk_usage', lambda path: SimpleNamespace(free=available * 1e9))
    engine = module.PostprocessOnlyEngine(store, abaqus='must-not-run-fe', fake=False)
    recovered = []
    monkeypatch.setattr(engine, '_postprocess', lambda r, attempt, b: recovered.append(attempt['path']))
    def legacy_admission(*args):
        raise AssertionError('Original FE wave admission must not reserve another native ODB')
    monkeypatch.setattr(orchestration.Engine, 'execute_wave', legacy_admission)
    engine.execute_wave([run], batch)
    assert recovered == [run['attempts'][0]['path']]


@pytest.mark.parametrize('resource', ['cpu', 'ram', 'disk'])
def test_postprocess_wave_still_enforces_original_resource_reserves(tmp_path, monkeypatch, resource):
    from types import SimpleNamespace

    import honeycomb_studio.engine as orchestration

    module = tool()
    store, design, _, _ = fixture(tmp_path)
    run, batch, _ = module.prepare_completed_run(store, {'design': design})
    monkeypatch.setattr(orchestration.psutil, 'cpu_count', lambda: 1 if resource == 'cpu' else 4)
    monkeypatch.setattr(orchestration.psutil, 'virtual_memory', lambda: SimpleNamespace(
        available=(1 if resource == 'ram' else 16) * 1024**3))
    monkeypatch.setattr(orchestration.shutil, 'disk_usage', lambda path: SimpleNamespace(
        free=(1 if resource == 'disk' else 1000) * 1e9))
    engine = module.PostprocessOnlyEngine(store, abaqus='must-not-run-fe', fake=False)
    with pytest.raises(RuntimeError, match='Insufficient CPU/RAM/export space'):
        engine.execute_wave([run], batch)


def test_postprocess_claim_rejects_a_duplicate_or_other_source():
    module = tool()
    work = dict(publication_kind='postprocess', publication_status='ready',
                workflow_run_id=123, source_sha='original')
    claim = module.claim_recovery(work, 456, 123, 'original')
    assert claim['publication_workflow_run_id'] == 456
    with pytest.raises(ValueError, match='exclusive'):
        module.claim_recovery(claim, 789, 123, 'original')
    with pytest.raises(ValueError, match='exclusive'):
        module.claim_recovery(work, 456, 123, 'different')


def test_failed_artifact_download_cannot_replace_the_immutable_original(tmp_path, monkeypatch):
    module = tool()
    work = dict(publication_workflow_run_id=456, workflow_run_id=123, source_sha='original',
                postprocess_artifact_run_id=123)
    updates = []

    class Catalog:
        def __init__(self, github):
            pass

        def get(self, key):
            return work, None

        def update(self, key, transform):
            updates.append(transform(work))

    monkeypatch.setattr(module, 'Catalog', Catalog)
    monkeypatch.setattr(module, 'Github', lambda **kwargs: object())
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module.sys, 'argv', ['campaign_postprocess.py', 'failure'])
    for key, value in dict(GITHUB_TOKEN='test', GITHUB_RUN_ID='456',
                           CAMPAIGN_ID='core-damage-v2-20261107', TICKET='a' * 32).items():
        monkeypatch.setenv(key, value)
    (tmp_path / 'partial.odb').write_bytes(b'incomplete transport bytes')
    module.main()
    assert not updates or updates[-1]['postprocess_artifact_run_id'] == 123


@pytest.mark.parametrize('receipt_run,retained,expected', [(456, True, 456), (123, True, 123), (456, False, 123)])
def test_only_this_attempts_completed_exports_can_replace_the_retained_artifact(tmp_path, monkeypatch,
                                                                              receipt_run, retained, expected):
    from types import SimpleNamespace

    from honeycomb_studio.store import write_json

    module = tool()
    work = dict(ticket='a' * 32, publication_workflow_run_id=456, workflow_run_id=123,
                source_sha='original', postprocess_artifact_run_id=123)
    updates = []

    class Catalog:
        def __init__(self, github):
            pass

        def get(self, key):
            return work, None

        def update(self, key, transform):
            updates.append(transform(work))

    artifact = dict(name='core-damage-' + 'a' * 32, expired=False)
    monkeypatch.setattr(module, 'Catalog', Catalog)
    monkeypatch.setattr(module, 'Github', lambda **kwargs: SimpleNamespace(repo=lambda *a:
                        {'artifacts': [artifact] if retained else []}))
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module.sys, 'argv', ['campaign_postprocess.py', 'failure'])
    for key, value in dict(GITHUB_TOKEN='test', GITHUB_RUN_ID='456',
                           CAMPAIGN_ID='core-damage-v2-20261107', TICKET='a' * 32).items():
        monkeypatch.setenv(key, value)
    write_json(tmp_path / 'campaign-execution.json', {**work, 'outcome': 'completed',
        'postprocess_workflow_run_id': receipt_run, 'new_fe_processes': 0, 'original_odb_sha256': 'a' * 64})
    module.main()
    assert (updates[-1] if updates else work)['postprocess_artifact_run_id'] == expected


def test_controller_retries_postprocessing_with_separate_original_and_tool_revisions(monkeypatch):
    tool()
    import campaign_controller
    work = dict(ticket='a' * 32, workflow_run_id=123, source_sha='original-source',
                publication_workflow_run_id=456, publication_attempt=1,
                publication_kind='postprocess', postprocess_source_sha='recovery-tool-source',
                postprocess_artifact_run_id=987)
    calls = []

    class Github:
        def repo(self, method, path, body=None):
            calls.append((method, path, body))
            if 'artifacts?' in path:
                return {'artifacts': [{'id': 100, 'name': 'retained', 'expired': False}]}
            return {'status': 'completed', 'conclusion': 'failure'}

    class Catalog:
        def __init__(self, github):
            pass

        def get(self, key):
            return work, None

        def update(self, key, transform):
            return transform(work)

    monkeypatch.setattr(campaign_controller, 'Catalog', Catalog)
    from types import SimpleNamespace
    app = SimpleNamespace(repository=lambda repo: Github())
    row = dict(repository='owner/repo', publication_ticket='a' * 32, recoverable_artifact='retained')
    campaign_controller.reconcile_publication({'id': 'core-damage-v2-20261107'}, row, app)
    posted = [c for c in calls if c[0] == 'POST']
    assert len(posted) == 1 and posted[0][1].endswith('core-damage-postprocess.yml/dispatches')
    assert posted[0][2]['inputs'] == dict(campaign_id='core-damage-v2-20261107', ticket='a' * 32,
        source_sha='original-source', original_run_id='123',
        processor_source_sha='recovery-tool-source', artifact_run_id='987')


@pytest.mark.parametrize('probe,visible_controller,expected', [(True, False, 0), (False, False, 1), (True, True, 0)])
def test_refill_preserves_controller_slot_even_before_its_job_is_visible(monkeypatch, probe, visible_controller, expected):
    tool()
    from types import SimpleNamespace

    import campaign_controller
    jobs = [dict(status='in_progress', campaign_solver=True) for _ in range(18)]
    if probe:
        jobs.append(dict(status='in_progress', campaign_solver=False, workflow_path='.github/workflows/core-damage-bond-probe.yml'))
    if visible_controller:
        jobs.append(dict(status='in_progress', campaign_solver=False, workflow_path='.github/workflows/core-damage-controller.yml'))
    reserved = []
    monkeypatch.setattr(campaign_controller, 'page_jobs', lambda *args, **kwargs: jobs)
    monkeypatch.setattr(campaign_controller, 'admission', lambda *args: True)
    monkeypatch.setattr(campaign_controller, 'Dispatcher', lambda *args: SimpleNamespace(reserve=lambda m, r: reserved.append(r)))
    rows = [dict(owner='xuankhanh24', status='pending') for _ in range(3)]
    app = SimpleNamespace(repository=lambda repo: object())
    campaign_controller.fill_owner_slots(None, app, {'designs': [], 'pilot_designs': []}, rows, 160, 'xuankhanh24')
    assert len(reserved) == expected


@pytest.mark.parametrize('artifact_state', ['completed', 'partial', 'unavailable'])
def test_finished_extraction_failure_is_not_automatically_recomputed(monkeypatch, artifact_state):
    tool()
    from types import SimpleNamespace

    import campaign_controller

    design = record(reference_model(), 'development', 1, 'xuankhanh24', [])
    design.update(status='dispatching', ticket='a' * 32, dispatch_started_at=0)
    work = dict(ticket='a' * 32, source_sha='original', workflow_run_id=123,
                status='finished', outcome='solver_failed', design=design,
                error='Workflow stopped before a verified execution receipt; inspect Actions diagnostics')
    updates = []

    class Catalog:
        def __init__(self, github):
            pass

        def get(self, key):
            return work, None

        def update(self, key, transform):
            updates.append(transform(work))

    monkeypatch.setattr(campaign_controller, 'Catalog', Catalog)
    def inspect(*args):
        if artifact_state == 'unavailable':
            raise ValueError('Artifact must be inspected')
        return {'solver_success': True} if artifact_state == 'completed' else None

    monkeypatch.setattr(campaign_controller, 'completed_odb_evidence', inspect, raising=False)
    monkeypatch.setattr(campaign_controller, 'controller_source_sha', lambda: 'recovery-tools', raising=False)
    github = SimpleNamespace(repo=lambda *a: {'status': 'completed'})
    app = SimpleNamespace(repository=lambda repo: github)
    manifest = dict(id='core-damage-v2-20261107', designs=[design], pilot_designs=[])
    campaign_controller.reconcile_owner(None, manifest, app, 'xuankhanh24')
    if artifact_state != 'completed':
        assert design['status'] == ('solver_retry' if artifact_state == 'partial' else 'needs_investigation')
        assert not updates
        return
    assert design['status'] == 'publication_pending'
    assert design['publication_ticket'] == 'a' * 32
    assert design.get('attempt', 0) == 0
    assert updates[-1]['publication_kind'] == 'postprocess'
    assert updates[-1]['postprocess_source_sha'] == 'recovery-tools'
